#!/usr/bin/env python3
"""Monitor contínuo e filtrável de logcat para diagnósticos Android.

Perfis disponíveis: NFC, display, botões, sensores e proximidade.

O programa mantém dois níveis de filtro:

1. Regex no host, em Python. Este é o padrão e preserva tags de OEM, HAL e
   driver que não façam parte do AOSP.
2. Filtro opcional dentro do próprio logcat. Reduz o volume no ADB, mas uma
   allowlist de tags pode esconder evidências do fornecedor.

Por padrão o script não limpa buffers e não altera propriedades log.tag.
Todos os comandos são listas passadas a subprocess, sem shell local.

Referências oficiais:
  https://developer.android.com/tools/logcat
  https://android.googlesource.com/platform/system/logging/+/refs/heads/main/logcat/logcat.cpp
  https://source.android.com/docs/core/interaction/input/getevent

Exemplos:
  python logcat_watch.py --profile nfc
  python logcat_watch.py --profile display --duration 120
  python logcat_watch.py --profile sensors --match '(?i)proximity|i2c'
  python logcat_watch.py --profile buttons --enable-aosp-debug
  python logcat_watch.py --raw-input --input-device /dev/input/event1

Use Ctrl+C para finalizar uma coleta contínua.
"""

from __future__ import annotations

import argparse
import queue
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Pattern, Sequence

from common_adb import (
    AdbRunner,
    CommandResult,
    add_adb_arguments,
    create_output_dir,
    run_and_save,
    runner_from_args,
    utc_now,
    write_json,
    write_text,
)


# A documentação oficial lista estes buffers. O perfil padrão acrescenta
# "events" ao conjunto default para trazer contexto de sistema quando existir.
DEFAULT_BUFFERS = ("main", "system", "crash", "events")
VALID_BUFFERS = frozenset(
    {"main", "system", "crash", "events", "radio", "all", "default"}
)

TAG_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
PROPERTY_NAME_RE = re.compile(r"^(?:persist\.)?log\.tag\.[A-Za-z0-9_.-]+$")
INPUT_EVENT_PATH_RE = re.compile(r"^/dev/input/event[0-9]+$")


@dataclass(frozen=True)
class WatchProfile:
    """Assunto com tags AOSP conhecidas e regex local ampla."""

    name: str
    description: str
    aosp_tags: tuple[str, ...]
    host_regex: str


# Tags não são uma API estável de OEM. Por isso o script não aplica a allowlist
# abaixo no device a menos que o usuário peça --device-tag-filter.
PROFILES: dict[str, WatchProfile] = {
    "all": WatchProfile(
        name="all",
        description="Mostra tudo; recomendado para descobrir tags de um aparelho novo.",
        aosp_tags=(),
        host_regex="",
    ),
    "nfc": WatchProfile(
        name="nfc",
        description="NfcService, NCI, descoberta RF, tag/NDEF, ISO-DEP e HCE.",
        aosp_tags=(
            "NfcService",
            "NfcDispatcher",
            "NativeNfcManager",
            "NfcTag",
            "HostEmulationManager",
            "CardEmulationManager",
            "RegisteredAidCache",
            "AidRoutingManager",
        ),
        host_regex=(
            r"(?i)\b(?:nfc|nci|nfcc|nfcservice|nfcdispatcher|"
            r"nativenfcmanager|ndef|iso[-_ ]?dep|rf[-_ ]?discovery|"
            r"tag[-_ ]?(?:discovered|lost)|transceive|apdu|polling|"
            r"listening|activation|deactivation|hce|aid)\b"
        ),
    ),
    "display": WatchProfile(
        name="display",
        description="Composição, energia, painel, brilho, DRM/DSI/MIPI, I2C e flicker.",
        aosp_tags=(
            "SurfaceFlinger",
            "DisplayManagerService",
            "DisplayPowerController",
            "DisplayPowerState",
            "DisplayDevice",
            "HWComposer",
            "Composer",
            "Scheduler",
            "BufferQueueProducer",
            "WindowManager",
        ),
        host_regex=(
            r"(?i)\b(?:surfaceflinger|display(?:power|manager|device|state)?|"
            r"hwc(?:omposer)?|composer|bufferqueue|drm|dsi|mipi|panel|ddic|"
            r"flicker|vsync|refresh(?:rate)?|brightness|backlight|i2c)\b"
        ),
    ),
    "buttons": WatchProfile(
        name="buttons",
        description="InputReader/InputDispatcher e eventos de tecla ou touch logados.",
        aosp_tags=(
            "InputReader",
            "InputDispatcher",
            "InputManager",
            "WindowManager",
            "ActivityTaskManager",
            "ViewRootImpl",
            "InputMethodManager",
        ),
        host_regex=(
            r"(?i)\b(?:input(?:reader|dispatcher|manager)?|keycode_[a-z0-9_]+|"
            r"dispatch(?:key|touch)|motionevent|action_(?:down|up|move|cancel)|"
            r"onclick|touch|tap|button|key(?:event)?|volume(?:_up|_down)?|power)\b"
        ),
    ),
    "sensors": WatchProfile(
        name="sensors",
        description="SensorService, HAL AIDL, privacidade, sensores físicos e I2C.",
        aosp_tags=(
            "SensorService",
            "AidlSensorManager",
            "NativeSensorService",
            "Sensors",
            "SensorManager",
            "SensorPrivacyService",
            "ProxSensor",
            "DisplayPowerController",
        ),
        host_regex=(
            r"(?i)\b(?:sensor(?:service|manager|privacy)?|aidlsensormanager|"
            r"nativesensorservice|proximity|prox(?:_sensor)?|light(?:sensor)?|"
            r"accelerometer|gyroscope|magnetometer|hinge|hall|i2c)\b"
        ),
    ),
    "proximity": WatchProfile(
        name="proximity",
        description="Proximidade correlacionada com a decisão de energia da tela.",
        aosp_tags=(
            "SensorService",
            "AidlSensorManager",
            "NativeSensorService",
            "Sensors",
            "SensorManager",
            "ProxSensor",
            "DisplayPowerController",
            "PowerManagerService",
        ),
        host_regex=(
            r"(?i)\b(?:proximity|prox(?:_sensor)?|near|far|"
            r"displaypowercontroller|powermanager|sensorservice)\b"
        ),
    ),
}

PROFILE_PATTERNS: dict[str, Pattern[str] | None] = {
    name: re.compile(profile.host_regex) if profile.host_regex else None
    for name, profile in PROFILES.items()
}

# getevent é diferente de logcat. Ele exibe eventos raw do kernel e é o caminho
# AOSP adequado quando um click/touch não aparece em logcat.
RAW_INPUT_INTEREST_RE = re.compile(
    r"\b(?:EV_KEY|KEY_[A-Z0-9_]+|EV_ABS|ABS_MT_[A-Z0-9_]+|"
    r"ABS_PRESSURE|EV_SYN|SYN_REPORT)\b"
)


@dataclass(frozen=True)
class LogcatCapabilities:
    """Capacidades anunciadas pelo logcat conectado ao aparelho."""

    supports_pid: bool
    supports_regex: bool
    supports_following_tail: bool


@dataclass(frozen=True)
class StreamStats:
    """Resumo reproduzível de uma execução de stream."""

    source: str
    started_at_utc: str
    finished_at_utc: str
    total_lines: int
    matched_lines: int
    interrupted: bool
    process_returncode: int | None


def compile_patterns(expressions: Sequence[str]) -> tuple[Pattern[str], ...]:
    """Compila regex locais antes de iniciar a sessão ADB."""

    compiled: list[Pattern[str]] = []
    for expression in expressions:
        try:
            compiled.append(re.compile(expression))
        except re.error as exc:
            raise ValueError(f"Regex local inválida: {expression!r}: {exc}") from exc
    return tuple(compiled)


def grep_line(
    line: str,
    includes: Sequence[Pattern[str]],
    excludes: Sequence[Pattern[str]] = (),
) -> bool:
    """Equivalente local de grep para uma única linha.

    Uma linha passa quando atende pelo menos uma regex include, ou quando não
    há include, e não atende nenhuma regex exclude. Filtrar no host evita
    depender da versão de grep ou toybox instalada no telefone.
    """

    if any(pattern.search(line) for pattern in excludes):
        return False
    return not includes or any(pattern.search(line) for pattern in includes)


def grep_profile(profile_name: str, line: str) -> bool:
    """Aplica apenas o padrão local de um perfil."""

    pattern = PROFILE_PATTERNS[profile_name]
    return pattern is None or bool(pattern.search(line))


def grep_nfc(line: str) -> bool:
    """Seleciona evidências NFC, NCI, NDEF, ISO-DEP ou HCE."""

    return grep_profile("nfc", line)


def grep_display(line: str) -> bool:
    """Seleciona evidências de display, painel e composição."""

    return grep_profile("display", line)


def grep_buttons(line: str) -> bool:
    """Seleciona evidências de input que chegaram ao logcat."""

    return grep_profile("buttons", line)


def grep_sensors(line: str) -> bool:
    """Seleciona SensorService, AIDL e sensores físicos."""

    return grep_profile("sensors", line)


def grep_proximity(line: str) -> bool:
    """Seleciona proximidade e seu efeito em DisplayPowerController."""

    return grep_profile("proximity", line)


def parse_buffers(value: str) -> tuple[str, ...]:
    """Valida buffers documentados antes de montar a chamada."""

    buffers = tuple(item.strip() for item in value.split(",") if item.strip())
    if not buffers:
        raise ValueError("--buffers precisa conter ao menos um buffer.")
    invalid = [item for item in buffers if item not in VALID_BUFFERS]
    if invalid:
        raise ValueError(
            "Buffer(s) inválido(s): "
            + ", ".join(invalid)
            + ". Use: "
            + ", ".join(sorted(VALID_BUFFERS))
        )
    if "all" in buffers and len(buffers) != 1:
        raise ValueError("Use --buffers all sozinho, pois ele já contém todos os buffers.")
    if "default" in buffers and len(buffers) != 1:
        raise ValueError(
            "Use --buffers default sozinho, pois ele já contém main, system e crash."
        )
    return buffers


def get_logcat_capabilities(runner: AdbRunner, output_dir: Path) -> LogcatCapabilities:
    """Executa logcat --help e usa a saída do aparelho como fonte de verdade."""

    result = run_and_save(runner, output_dir, "logcat_help", ["logcat", "--help"])
    help_text = result.stdout + "\n" + result.stderr
    if result.returncode != 0:
        raise RuntimeError(
            "Não foi possível executar logcat --help. Confirme a conexão ADB."
        )

    return LogcatCapabilities(
        supports_pid="--pid" in help_text,
        supports_regex="--regex" in help_text,
        supports_following_tail=bool(re.search(r"(?m)^\s*-T\b", help_text)),
    )


def build_tag_filter_args(tags: Sequence[str], minimum_priority: str = "V") -> list[str]:
    """Monta filtros oficiais do tipo TAG:priority seguidos por *:S."""

    priority = minimum_priority.upper()
    if priority not in {"V", "D", "I", "W", "E", "F"}:
        raise ValueError("A prioridade de tag deve ser V, D, I, W, E ou F.")

    unique_tags = tuple(dict.fromkeys(tags))
    for tag in unique_tags:
        if not TAG_NAME_RE.fullmatch(tag):
            raise ValueError(
                f"Tag inválida para filtro no device: {tag!r}. "
                "Aceitos: letras, números, ponto, hífen e sublinhado."
            )

    if not unique_tags:
        return []

    # *:S faz a expressão atuar como allowlist, conforme a documentação do
    # Android Developers. Use somente se aceitar perder tags OEM desconhecidas.
    return [f"{tag}:{priority}" for tag in unique_tags] + ["*:S"]


def build_logcat_args(
    *,
    buffers: Sequence[str],
    capabilities: LogcatCapabilities,
    tail_lines: int = 0,
    pid: int | None = None,
    device_regex: str | None = None,
    tag_filters: Sequence[str] = (),
    tag_priority: str = "V",
    event_descriptions: bool = False,
) -> list[str]:
    """Cria argumentos de logcat validados contra o help do device.

    A implementação AOSP atual aplica --regex à mensagem do log, não à tag.
    Para combinar tag e conteúdo, use os filtros locais do perfil.
    """

    args = ["logcat"]
    for buffer_name in buffers:
        args.extend(["-b", buffer_name])
    args.extend(["-v", "threadtime"])
    if event_descriptions:
        args.extend(["-v", "descriptive"])

    if tail_lines:
        if tail_lines < 0:
            raise ValueError("--tail deve ser zero ou positivo.")
        if not capabilities.supports_following_tail:
            raise RuntimeError("Este logcat não anunciou a opção -T.")
        args.extend(["-T", str(tail_lines)])

    if pid is not None:
        if pid <= 0:
            raise ValueError("--pid deve ser positivo.")
        if not capabilities.supports_pid:
            raise RuntimeError("Este logcat não anunciou suporte a --pid.")
        # A implementação AOSP atual aceita um PID. Se o serviço renascer,
        # inicie outra coleta com o novo PID para não perder os eventos.
        args.append(f"--pid={pid}")

    if device_regex:
        if not capabilities.supports_regex:
            raise RuntimeError("Este logcat não anunciou suporte a --regex.")
        # A regex abaixo é ECMAScript no AOSP, logo não assuma sintaxe Python.
        args.extend(["--regex", device_regex])

    args.extend(build_tag_filter_args(tag_filters, tag_priority))
    return args


class StreamWatcher:
    """Executa um stream ADB e escreve raw e recorte sem bloquear o loop."""

    def __init__(
        self,
        runner: AdbRunner,
        command: Sequence[str],
        output_dir: Path,
        *,
        source_name: str,
        matcher: Sequence[Pattern[str]] = (),
        excludes: Sequence[Pattern[str]] = (),
        save_raw: bool = True,
        quiet: bool = False,
    ) -> None:
        self.runner = runner
        self.command = list(command)
        self.output_dir = output_dir
        self.source_name = source_name
        self.matcher = tuple(matcher)
        self.excludes = tuple(excludes)
        self.save_raw = save_raw
        self.quiet = quiet
        self.process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str] = queue.Queue(maxsize=4096)
        self._reader: threading.Thread | None = None
        self._stop_requested = threading.Event()

    def start(self) -> None:
        self.process = self.runner.popen(self.command)
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        """Lê em thread para que duração e Ctrl+C não dependam de nova linha."""

        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            while not self._stop_requested.is_set():
                try:
                    self._lines.put(line, timeout=0.20)
                    break
                except queue.Full:
                    # Preserva memória limitada sob um logcat muito verboso.
                    continue

    def _stop_process(self) -> None:
        self._stop_requested.set()
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self._reader is not None:
            self._reader.join(timeout=1)

    def _record_line(self, line: str, raw_file, matches_file) -> bool:
        if raw_file is not None:
            raw_file.write(line)
        matched = grep_line(line, self.matcher, self.excludes)
        if matched:
            matches_file.write(line)
            if not self.quiet:
                print(line, end="", flush=True)
        return matched

    def run(self, duration_seconds: float | None) -> StreamStats:
        """Mantém a coleta até o tempo acabar, o processo morrer ou Ctrl+C."""

        self.output_dir.mkdir(parents=True, exist_ok=True)
        raw_path = self.output_dir / f"{self.source_name}.raw.txt"
        matches_path = self.output_dir / f"{self.source_name}.matches.txt"
        started_at = utc_now()
        total_lines = 0
        matched_lines = 0
        interrupted = False
        deadline = (
            time.monotonic() + duration_seconds if duration_seconds is not None else None
        )
        raw_file = (
            raw_path.open("a", encoding="utf-8", errors="replace", buffering=1)
            if self.save_raw
            else None
        )
        matches_file = matches_path.open(
            "a", encoding="utf-8", errors="replace", buffering=1
        )

        try:
            self.start()
            while True:
                if deadline is None:
                    timeout = 0.20
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    timeout = min(0.20, remaining)

                try:
                    line = self._lines.get(timeout=timeout)
                except queue.Empty:
                    if self.process is not None and self.process.poll() is not None:
                        break
                    continue

                total_lines += 1
                if self._record_line(line, raw_file, matches_file):
                    matched_lines += 1
        except KeyboardInterrupt:
            interrupted = True
        finally:
            self._stop_process()
            # Grava linhas que chegaram pouco antes de encerrar adb.
            while True:
                try:
                    line = self._lines.get_nowait()
                except queue.Empty:
                    break
                total_lines += 1
                if self._record_line(line, raw_file, matches_file):
                    matched_lines += 1

            if raw_file is not None:
                raw_file.close()
            matches_file.close()

        return StreamStats(
            source=self.source_name,
            started_at_utc=started_at,
            finished_at_utc=utc_now(),
            total_lines=total_lines,
            matched_lines=matched_lines,
            interrupted=interrupted,
            process_returncode=self.process.returncode if self.process is not None else None,
        )


def write_watch_manifest(
    output_dir: Path,
    *,
    source: str,
    runner: AdbRunner,
    command: Sequence[str],
    profile: str | None,
    includes: Sequence[str],
    excludes: Sequence[str],
    stats: StreamStats,
) -> None:
    """Grava comando, serial e regex para permitir repetição do teste."""

    write_json(
        output_dir / f"{source}.watch.json",
        {
            "captured_at_utc": utc_now(),
            "serial": runner.serial,
            "profile": profile,
            "host_include_regex": list(includes),
            "host_exclude_regex": list(excludes),
            "adb_argv": runner.argv(*command),
            "stats": asdict(stats),
        },
    )


def watch_logcat_profile(
    runner: AdbRunner,
    output_dir: Path,
    profile_name: str,
    *,
    duration_seconds: float | None = None,
    buffers: Sequence[str] = DEFAULT_BUFFERS,
    additional_matches: Sequence[str] = (),
    exclusions: Sequence[str] = (),
    tail_lines: int = 0,
    pid: int | None = None,
    device_regex: str | None = None,
    device_tag_filter: bool = False,
    extra_tags: Sequence[str] = (),
    tag_priority: str = "V",
    event_descriptions: bool = False,
    save_raw: bool = True,
    quiet: bool = False,
) -> StreamStats:
    """Função genérica para observar um perfil no logcat."""

    if profile_name not in PROFILES:
        raise ValueError(f"Perfil inexistente: {profile_name}.")

    profile = PROFILES[profile_name]
    capabilities = get_logcat_capabilities(runner, output_dir)
    include_expressions: list[str] = []
    if profile.host_regex:
        include_expressions.append(profile.host_regex)
    include_expressions.extend(additional_matches)

    tag_filters: list[str] = list(extra_tags)
    if device_tag_filter:
        tag_filters = [*profile.aosp_tags, *tag_filters]

    command = build_logcat_args(
        buffers=buffers,
        capabilities=capabilities,
        tail_lines=tail_lines,
        pid=pid,
        device_regex=device_regex,
        tag_filters=tag_filters,
        tag_priority=tag_priority,
        event_descriptions=event_descriptions,
    )
    write_text(
        output_dir / "README.txt",
        (
            "Coleta observacional: nenhum buffer logcat foi limpo.\n"
            "O arquivo logcat.raw.txt guarda tudo recebido; "
            "logcat.matches.txt guarda o recorte local.\n"
            "Filtro por tag no device pode excluir logs proprietários.\n"
        ),
    )
    watcher = StreamWatcher(
        runner,
        command,
        output_dir,
        source_name="logcat",
        matcher=compile_patterns(include_expressions),
        excludes=compile_patterns(exclusions),
        save_raw=save_raw,
        quiet=quiet,
    )
    stats = watcher.run(duration_seconds)
    write_watch_manifest(
        output_dir,
        source="logcat",
        runner=runner,
        command=command,
        profile=profile_name,
        includes=include_expressions,
        excludes=exclusions,
        stats=stats,
    )
    return stats


def watch_nfc(runner: AdbRunner, output_dir: Path, **kwargs) -> StreamStats:
    """Atalho para NfcService, NCI, tag e HCE."""

    return watch_logcat_profile(runner, output_dir, "nfc", **kwargs)


def watch_display(runner: AdbRunner, output_dir: Path, **kwargs) -> StreamStats:
    """Atalho para SurfaceFlinger, display e painel."""

    return watch_logcat_profile(runner, output_dir, "display", **kwargs)


def watch_buttons(runner: AdbRunner, output_dir: Path, **kwargs) -> StreamStats:
    """Atalho para InputReader, InputDispatcher e eventos de botão."""

    return watch_logcat_profile(runner, output_dir, "buttons", **kwargs)


def watch_sensors(runner: AdbRunner, output_dir: Path, **kwargs) -> StreamStats:
    """Atalho para SensorService, HAL AIDL e sensores físicos."""

    return watch_logcat_profile(runner, output_dir, "sensors", **kwargs)


def watch_proximity(runner: AdbRunner, output_dir: Path, **kwargs) -> StreamStats:
    """Atalho para proximidade e DisplayPowerController."""

    return watch_logcat_profile(runner, output_dir, "proximity", **kwargs)


def set_log_property(runner: AdbRunner, name: str, value: str) -> CommandResult:
    """Aplica explicitamente uma propriedade log.tag sem shell local."""

    if not PROPERTY_NAME_RE.fullmatch(name):
        raise ValueError(f"Nome de propriedade inválido: {name!r}")
    if value not in {"VERBOSE", "DEBUG", "INFO", "WARN", "ERROR", "SILENT"}:
        raise ValueError(f"Nível de log inválido: {value!r}")
    return runner.shell("setprop", name, value)


def enable_input_dispatcher_debug(runner: AdbRunner) -> list[CommandResult]:
    """Ativa detalhes AOSP de InputDispatcher em modo opt-in.

    O código AOSP atual descreve essas propriedades. Alguns builds requerem
    reinício do serviço ou do aparelho; esta função nunca reinicia o telefone.
    """

    properties = (
        "log.tag.InputDispatcherInboundEvent",
        "log.tag.InputDispatcherOutboundEvent",
        "log.tag.InputDispatcherDispatchCycle",
        "log.tag.InputDispatcherInjection",
        "log.tag.InputDispatcherFocus",
    )
    return [set_log_property(runner, name, "DEBUG") for name in properties]


def enable_display_framework_debug(runner: AdbRunner) -> list[CommandResult]:
    """Ativa logs AOSP persistentes de display, sem executar reboot.

    DisplayManagerService e DisplayPowerController registram no AOSP o uso de
    persist.log.tag seguido de reboot. Reboot fica fora deste script para não
    alterar o estado do teste sem uma ação explícita do operador.
    """

    properties = (
        "persist.log.tag.DisplayManagerService",
        "persist.log.tag.DisplayPowerController",
    )
    return [set_log_property(runner, name, "DEBUG") for name in properties]


def enable_profile_debug(runner: AdbRunner, profile_name: str) -> list[CommandResult]:
    """Aplica apenas toggles AOSP conhecidos para o perfil informado."""

    if profile_name == "buttons":
        return enable_input_dispatcher_debug(runner)
    if profile_name in {"display", "proximity"}:
        return enable_display_framework_debug(runner)
    return []


def list_input_devices(
    runner: AdbRunner, output_dir: Path, *, use_su: bool = True
) -> CommandResult:
    """Salva capacidades dos dispositivos Linux expostos em /dev/input."""

    prefix = ["shell", "su", "--"] if use_su else ["shell"]
    return run_and_save(
        runner,
        output_dir,
        "getevent_capabilities",
        [*prefix, "getevent", "-lp"],
        timeout=30.0,
    )


def watch_raw_input_events(
    runner: AdbRunner,
    output_dir: Path,
    *,
    input_device: str | None = None,
    use_su: bool = True,
    duration_seconds: float | None = None,
    all_events: bool = False,
    save_raw: bool = True,
    quiet: bool = False,
) -> StreamStats:
    """Observa getevent -lt para provar que o evento chegou ao kernel.

    Essa função complementa o perfil buttons. Em aparelhos sem root, /dev/input
    pode ser inacessível; a mensagem de falha será preservada no arquivo raw.
    """

    if input_device is not None and not INPUT_EVENT_PATH_RE.fullmatch(input_device):
        raise ValueError(
            "--input-device deve ser /dev/input/eventN, por exemplo /dev/input/event1."
        )

    command = ["shell"]
    if use_su:
        command.extend(["su", "--"])
    command.extend(["getevent", "-lt"])
    if input_device:
        command.append(input_device)

    write_text(
        output_dir / "README.txt",
        (
            "Coleta raw de kernel com getevent, não um buffer de logcat.\n"
            "Use getevent_capabilities.stdout.txt para mapear eventN ao hardware.\n"
        ),
    )
    watcher = StreamWatcher(
        runner,
        command,
        output_dir,
        source_name="getevent",
        matcher=() if all_events else (RAW_INPUT_INTEREST_RE,),
        save_raw=save_raw,
        quiet=quiet,
    )
    stats = watcher.run(duration_seconds)
    write_watch_manifest(
        output_dir,
        source="getevent",
        runner=runner,
        command=command,
        profile=None,
        includes=[] if all_events else [RAW_INPUT_INTEREST_RE.pattern],
        excludes=[],
        stats=stats,
    )
    return stats


def build_parser() -> argparse.ArgumentParser:
    profile_help = "\n".join(
        f"  {name:<10} {profile.description}" for name, profile in PROFILES.items()
    )
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Perfis disponíveis:\n"
            f"{profile_help}\n\n"
            "Atenção: --device-tag-filter economiza tráfego, mas pode esconder "
            "tags proprietárias que não estejam na allowlist AOSP."
        ),
    )
    add_adb_arguments(parser)
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        default="all",
        help="Perfil de regex local e tags AOSP conhecidas.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="Segundos de coleta; 0 mantém a sessão até Ctrl+C.",
    )
    parser.add_argument(
        "--buffers",
        default=",".join(DEFAULT_BUFFERS),
        help="Buffers separados por vírgula. Padrão: main,system,crash,events.",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=0,
        help="Inclui N linhas recentes e continua seguindo com -T.",
    )
    parser.add_argument(
        "--pid",
        type=int,
        help="Usa --pid somente se o logcat do aparelho anunciar suporte.",
    )
    parser.add_argument(
        "--match",
        action="append",
        default=[],
        help="Regex Python adicional combinada com OR ao perfil; pode repetir.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Regex Python excluída após o perfil; pode repetir.",
    )
    parser.add_argument(
        "--device-regex",
        help=(
            "Regex ECMAScript passada a logcat --regex quando suportada. "
            "Ela filtra mensagem; --match continua sendo regex Python local."
        ),
    )
    parser.add_argument(
        "--device-tag-filter",
        action="store_true",
        help="Envia as tags AOSP do perfil ao logcat com a allowlist *:S.",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Tag adicional da allowlist logcat; também ativa filtro no device.",
    )
    parser.add_argument(
        "--tag-priority",
        choices=["V", "D", "I", "W", "E", "F"],
        default="V",
        help="Prioridade mínima no filtro por tag.",
    )
    parser.add_argument(
        "--event-descriptions",
        action="store_true",
        help="Adiciona o modificador descriptive para decodificar o buffer events.",
    )
    parser.add_argument(
        "--no-raw",
        action="store_true",
        help="Não cria arquivo raw; grava apenas linhas selecionadas.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Grava arquivos sem imprimir linhas selecionadas no terminal.",
    )
    parser.add_argument(
        "--enable-aosp-debug",
        action="store_true",
        help=(
            "Opt-in: executa setprop de debug AOSP para buttons ou display/proximity. "
            "Não reinicia o aparelho."
        ),
    )
    parser.add_argument(
        "--list-input-devices",
        action="store_true",
        help="Executa getevent -lp e salva capacidades de /dev/input.",
    )
    parser.add_argument(
        "--raw-input",
        action="store_true",
        help="Observa getevent -lt em vez de logcat.",
    )
    parser.add_argument(
        "--input-device",
        help="Limita getevent a /dev/input/eventN; veja --list-input-devices.",
    )
    parser.add_argument(
        "--without-su",
        action="store_true",
        help="Em getevent, não usa o prefixo su --.",
    )
    parser.add_argument(
        "--all-input-events",
        action="store_true",
        help="Em --raw-input, imprime também eventos fora de tecla/touch/EV_SYN.",
    )
    return parser


def save_debug_results(output_dir: Path, results: Sequence[CommandResult]) -> None:
    """Preserva stdout, stderr e código de retorno de cada setprop opt-in."""

    write_json(
        output_dir / "debug_properties.json",
        {"results": [result.to_json() for result in results]},
    )


def main() -> int:
    args = build_parser().parse_args()
    if args.duration < 0:
        raise SystemExit("--duration não pode ser negativa.")
    if args.raw_input and args.enable_aosp_debug:
        raise SystemExit("--enable-aosp-debug só se aplica a logcat, não a getevent.")

    runner = runner_from_args(args)
    source_prefix = "getevent" if args.raw_input else f"logcat_watch_{args.profile}"
    output_dir = create_output_dir(args.out, source_prefix, runner.serial)
    duration = None if args.duration == 0 else args.duration

    try:
        if args.list_input_devices:
            result = list_input_devices(
                runner, output_dir, use_su=not args.without_su
            )
            print(f"Capacidades salvas em {output_dir}")
            return 0 if result.returncode == 0 else result.returncode

        if args.raw_input:
            stats = watch_raw_input_events(
                runner,
                output_dir,
                input_device=args.input_device,
                use_su=not args.without_su,
                duration_seconds=duration,
                all_events=args.all_input_events,
                save_raw=not args.no_raw,
                quiet=args.quiet,
            )
        else:
            buffers = parse_buffers(args.buffers)
            if args.enable_aosp_debug:
                results = enable_profile_debug(runner, args.profile)
                save_debug_results(output_dir, results)
                failures = [result for result in results if result.returncode != 0]
                if failures:
                    raise RuntimeError(
                        "Uma ou mais propriedades de debug falharam. "
                        "Veja debug_properties.json."
                    )
                if results:
                    print(
                        "Propriedades de debug aplicadas. Alguns builds exigem "
                        "reiniciar o aparelho ou serviço para produzir os logs."
                    )
                else:
                    print(
                        "Este perfil não tem toggle AOSP conhecido; a coleta é observacional."
                    )

            stats = watch_logcat_profile(
                runner,
                output_dir,
                args.profile,
                duration_seconds=duration,
                buffers=buffers,
                additional_matches=args.match,
                exclusions=args.exclude,
                tail_lines=args.tail,
                pid=args.pid,
                device_regex=args.device_regex,
                device_tag_filter=args.device_tag_filter,
                extra_tags=args.tag,
                tag_priority=args.tag_priority,
                event_descriptions=args.event_descriptions,
                save_raw=not args.no_raw,
                quiet=args.quiet,
            )
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"Erro: {exc}") from exc

    print(
        f"\nColeta finalizada em {output_dir}\n"
        f"Linhas recebidas: {stats.total_lines}; selecionadas: {stats.matched_lines}; "
        f"interrompida: {stats.interrupted}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
