#!/usr/bin/env python3
"""Primitivas seguras e reutilizáveis para diagnósticos Android via ADB.

O módulo usa ``adbutils`` apenas para descobrir/validar o dispositivo alvo e
``subprocess`` para executar cada comando ADB. Todos os comandos são listas de
argumentos; portanto, ``shell=True`` nunca é usado. Isso evita que um serial,
um caminho ou uma expressão de filtro seja interpretado pelo shell local.

Compatibilidade: os comandos ADB empregados aqui pertencem à interface ADB
documentada pelo Android Developers. A saída de ``dumpsys`` é diagnóstica e
não é um formato de API estável: ela é sempre preservada em arquivo bruto.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

try:
    import adbutils
except ImportError as exc:  # Mantém uma mensagem de instalação objetiva.
    adbutils = None  # type: ignore[assignment]
    ADBUTILS_IMPORT_ERROR = exc
else:
    ADBUTILS_IMPORT_ERROR = None


DEFAULT_TIMEOUT_SECONDS = 30.0
SAFE_FILE_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
SAFE_SYSFS_PATH = re.compile(r"^/sys/[A-Za-z0-9_./:-]+$")
SAFE_SYSFS_VALUE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


class AdbCommandError(RuntimeError):
    """Erro opcional para um comando que o chamador marcou como obrigatório."""


@dataclass(frozen=True)
class CommandResult:
    """Resultado completo de um subprocess ADB, pronto para auditoria."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    def to_json(self) -> dict[str, object]:
        return asdict(self)


class AdbRunner:
    """Executa ADB para exatamente um serial previamente validado.

    ``adbutils`` consulta o mesmo servidor ADB que o binário ``adb``. A seleção
    explícita por serial evita que uma coleta seja executada por engano em outro
    telefone quando vários dispositivos estão conectados.
    """

    def __init__(self, serial: str, adb_path: str = "adb") -> None:
        self.serial = serial
        self.adb_path = adb_path

    def argv(self, *args: str) -> list[str]:
        return [self.adb_path, "-s", self.serial, *args]

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        check: bool = False,
    ) -> CommandResult:
        """Executa um comando ADB sem shell local e captura stdout/stderr."""

        argv = self.argv(*args)
        try:
            completed = subprocess.run(
                argv,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(
                argv=argv,
                returncode=124,
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
            )
            if check:
                raise AdbCommandError(f"Tempo esgotado: {' '.join(argv)}") from exc
            return result

        result = CommandResult(
            argv=argv,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and result.returncode != 0:
            raise AdbCommandError(
                f"ADB retornou {result.returncode}: {' '.join(argv)}\n{result.stderr}"
            )
        return result

    def shell(
        self, *command: str, timeout: float = DEFAULT_TIMEOUT_SECONDS, check: bool = False
    ) -> CommandResult:
        """Executa um único comando no shell do dispositivo, por argumentos."""

        return self.run(["shell", *command], timeout=timeout, check=check)

    def popen(self, args: Sequence[str]) -> subprocess.Popen[str]:
        """Inicia uma coleta longa (logcat), ainda sem shell local."""

        return subprocess.Popen(
            self.argv(*args),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )


def _online_serials() -> list[str]:
    """Retorna seriais em estado ``device`` pelo cliente adbutils.

    ``adbutils`` é usado deliberadamente antes de qualquer subprocess: se o
    servidor ADB não estiver acessível ou houver mais de um alvo, a rotina para
    antes de coletar ou alterar algo no aparelho errado.
    """

    if adbutils is None:
        raise RuntimeError(
            "Dependência ausente: instale com `python -m pip install -r requirements.txt`. "
            f"Motivo original: {ADBUTILS_IMPORT_ERROR}"
        )

    # socket_timeout evita ficar preso indefinidamente se o servidor ADB cair.
    try:
        client = adbutils.AdbClient(host="127.0.0.1", port=5037, socket_timeout=10)
        serials: list[str] = []
        for device in client.device_list():
            serial = getattr(device, "serial", None)
            # device_list() da biblioteca já retorna somente state=device; o
            # fallback mantém compatibilidade com implementações que expõem state.
            state = getattr(device, "state", "device")
            if serial and state == "device":
                serials.append(str(serial))
        return serials
    except Exception as exc:
        raise RuntimeError(
            "Não foi possível consultar o servidor ADB em 127.0.0.1:5037. "
            "Confirme `adb devices -l` antes de rodar a coleta."
        ) from exc


def select_serial(requested_serial: str | None) -> str:
    """Seleciona um único dispositivo online, exigindo serial se necessário."""

    serials = _online_serials()
    if requested_serial:
        if requested_serial not in serials:
            shown = ", ".join(serials) if serials else "nenhum"
            raise RuntimeError(
                f"O serial solicitado ({requested_serial}) não está em estado 'device'. "
                f"Disponíveis: {shown}."
            )
        return requested_serial
    if len(serials) == 1:
        return serials[0]
    if not serials:
        raise RuntimeError("Nenhum dispositivo ADB em estado 'device' foi encontrado.")
    raise RuntimeError(
        "Há mais de um dispositivo ADB. Informe --serial para evitar coletar o alvo errado: "
        + ", ".join(serials)
    )


def add_adb_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--serial",
        help="Serial ADB do alvo. Obrigatório quando há mais de um dispositivo conectado.",
    )
    parser.add_argument(
        "--adb",
        default="adb",
        help="Caminho do binário adb (padrão: adb no PATH).",
    )
    parser.add_argument(
        "--out",
        default="artifacts",
        help="Diretório-raiz local para os artefatos (padrão: ./artifacts).",
    )


def runner_from_args(args: argparse.Namespace) -> AdbRunner:
    return AdbRunner(select_serial(args.serial), args.adb)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def safe_component(value: str) -> str:
    return SAFE_FILE_CHARS.sub("_", value).strip("._") or "device"


def create_output_dir(root: str | Path, prefix: str, serial: str) -> Path:
    """Cria diretório novo, sem apagar artefatos de uma execução anterior."""

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = Path(root).expanduser().resolve()
    candidate = base / f"{prefix}_{safe_component(serial)}_{timestamp}"
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = base / f"{prefix}_{safe_component(serial)}_{timestamp}_{suffix}"
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", errors="replace")


def write_json(path: Path, data: object) -> None:
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def save_command_result(output_dir: Path, label: str, result: CommandResult) -> None:
    """Salva a saída e os metadados sem perder stderr ou código de retorno."""

    stem = safe_component(label)
    write_text(output_dir / f"{stem}.stdout.txt", result.stdout)
    write_text(output_dir / f"{stem}.stderr.txt", result.stderr)
    write_json(output_dir / f"{stem}.command.json", result.to_json())


def run_and_save(
    runner: AdbRunner,
    output_dir: Path,
    label: str,
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> CommandResult:
    result = runner.run(args, timeout=timeout)
    save_command_result(output_dir, label, result)
    return result


def dump_services(runner: AdbRunner, output_dir: Path) -> set[str]:
    """Lista serviços via a sintaxe oficial ``dumpsys -l`` e retorna seus nomes."""

    result = run_and_save(runner, output_dir, "dumpsys_services", ["shell", "dumpsys", "-l"])
    services: set[str] = set()
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if not candidate or candidate.endswith(":") or " " in candidate:
            continue
        services.add(candidate)
    return services


def capture_dumpsys_if_present(
    runner: AdbRunner,
    output_dir: Path,
    services: set[str],
    service: str,
    *,
    label: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> CommandResult | None:
    """Coleta um serviço somente se ele foi anunciado pelo próprio dumpsys."""

    if service not in services:
        return None
    return run_and_save(
        runner,
        output_dir,
        label or f"dumpsys_{service}",
        ["shell", "dumpsys", service],
        timeout=timeout,
    )


def read_remote_file(runner: AdbRunner, path: str, *, timeout: float = 5.0) -> CommandResult:
    """Lê um arquivo remoto como argumento de ``cat``; não usa redirecionamento."""

    return runner.shell("cat", path, timeout=timeout)


def list_remote_dir(runner: AdbRunner, path: str, *, timeout: float = 5.0) -> CommandResult:
    """Lista um diretório remoto sem expandir curingas no host."""

    return runner.shell("ls", "-1", path, timeout=timeout)


def assert_safe_sysfs_path(path: str) -> None:
    if not SAFE_SYSFS_PATH.fullmatch(path):
        raise ValueError(
            "Caminho sysfs recusado. Use somente caminho absoluto em /sys/ com caracteres simples."
        )


def assert_safe_sysfs_value(value: str) -> None:
    if not SAFE_SYSFS_VALUE.fullmatch(value):
        raise ValueError("Valor sysfs recusado. Apenas números não negativos são aceitos.")


def unique_preserving_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
