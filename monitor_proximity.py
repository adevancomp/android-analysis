#!/usr/bin/env python3
"""Descobre e monitora o sensor de proximidade em Android recente.

O caminho AOSP é observado por ``dumpsys sensorservice`` e ``dumpsys power``.
Os caminhos em ``/sys`` são apenas pistas de implementação do fabricante: o
AOSP não especifica um ABI sysfs para proximidade. Por esse motivo, este script
somente lê por padrão. A escrita é uma transação única, exige confirmação e
aceita somente um nó que a própria descoberta conseguiu ler.
"""

from __future__ import annotations

import argparse
import csv
import re
import shlex
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from common_adb import (
    AdbRunner,
    add_adb_arguments,
    assert_safe_sysfs_path,
    assert_safe_sysfs_value,
    capture_dumpsys_if_present,
    create_output_dir,
    dump_services,
    list_remote_dir,
    read_remote_file,
    runner_from_args,
    run_and_save,
    utc_now,
    write_json,
    write_text,
)


COMMON_SYSFS_ROOTS = (
    "/sys/class/sensors",
    "/sys/devices/virtual/sensors",
    "/sys/bus/iio/devices",
)

# Somente os nomes da esquerda representam valores que podem variar durante a
# aproximação. Campos enable/threshold são preservados como metadados, mas não
# são escolhidos automaticamente para monitoramento.
VALUE_FILE_NAMES = {
    "raw_data",
    "state",
    "prox",
    "proximity",
    "value",
    "in_proximity_raw",
    "in_proximity_input",
}
METADATA_FILE_NAMES = {
    "name",
    "enable",
    "threshold",
    "thresh_high",
    "thresh_low",
    "in_proximity_scale",
    "in_proximity_offset",
    "in_proximity_enable",
}
PROX_WORD = re.compile(r"prox(?:imity)?", re.IGNORECASE)


@dataclass
class SysfsCandidate:
    base: str
    source_root: str
    driver_name: str | None
    readable: dict[str, str]
    value_paths: list[str]


def _directory_entries(runner: AdbRunner, path: str) -> list[str]:
    result = list_remote_dir(runner, path)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _read_value(runner: AdbRunner, path: str) -> tuple[bool, str]:
    result = read_remote_file(runner, path)
    if result.returncode != 0:
        return False, (result.stderr or f"cat retornou {result.returncode}").strip()
    return True, result.stdout.strip()


def _inspect_candidate(runner: AdbRunner, base: str, source_root: str) -> SysfsCandidate:
    entries = set(_directory_entries(runner, base))
    interesting = VALUE_FILE_NAMES | METADATA_FILE_NAMES
    readable: dict[str, str] = {}
    value_paths: list[str] = []
    for name in sorted(entries & interesting):
        path = f"{base}/{name}"
        ok, value = _read_value(runner, path)
        if ok:
            readable[name] = value
            if name in VALUE_FILE_NAMES:
                value_paths.append(path)
    driver_name = readable.get("name")
    return SysfsCandidate(
        base=base,
        source_root=source_root,
        driver_name=driver_name,
        readable=readable,
        value_paths=value_paths,
    )


def discover_sysfs_candidates(runner: AdbRunner) -> list[SysfsCandidate]:
    """Procura somente três árvores Linux comuns, sem varrer /sys inteiro."""

    candidates: list[SysfsCandidate] = []
    for root in COMMON_SYSFS_ROOTS:
        entries = _directory_entries(runner, root)
        if root.endswith("iio/devices"):
            for entry in entries:
                if not entry.startswith("iio:device"):
                    continue
                base = f"{root}/{entry}"
                children = set(_directory_entries(runner, base))
                has_prox_file = bool(children & VALUE_FILE_NAMES) or any(
                    name.startswith("in_proximity") for name in children
                )
                ok, driver_name = _read_value(runner, f"{base}/name")
                if has_prox_file or (ok and PROX_WORD.search(driver_name)):
                    candidates.append(_inspect_candidate(runner, base, root))
            continue

        for entry in entries:
            if not PROX_WORD.search(entry):
                continue
            base = f"{root}/{entry}"
            candidates.append(_inspect_candidate(runner, base, root))

    # Preserva a primeira ocorrência de cada base, sem confiar em layout fixo.
    seen: set[str] = set()
    unique: list[SysfsCandidate] = []
    for candidate in candidates:
        if candidate.base not in seen:
            unique.append(candidate)
            seen.add(candidate.base)
    return unique


def capture_framework_snapshot(
    runner: AdbRunner, output_dir: Path, services: set[str], index: int
) -> None:
    """Salva o estado AOSP e destaca linhas úteis sem descartar o dump bruto."""

    for service in ("sensorservice", "power"):
        result = capture_dumpsys_if_present(
            runner,
            output_dir,
            services,
            service,
            label=f"{index:04d}_dumpsys_{service}",
            timeout=20,
        )
        if result and service == "sensorservice":
            selected = [
                line
                for line in result.stdout.splitlines()
                if re.search(r"(?i)proximity|distance|android\.sensor\.proximity", line)
            ]
            write_text(
                output_dir / f"{index:04d}_sensorservice.proximity_excerpt.txt",
                "\n".join(selected) + ("\n" if selected else ""),
            )


def guarded_write(
    runner: AdbRunner,
    output_dir: Path,
    path: str,
    value: str,
    allowed_paths: set[str],
) -> None:
    """Executa uma escrita sysfs apenas após validação rígida e registra antes/depois.

    Redirecionamento é uma operação do shell remoto, por isso é inevitável usar
    ``sh -c`` aqui. Os dois campos são restringidos por regex e citados com
    ``shlex.quote``; o subprocess local continua sem shell.
    """

    assert_safe_sysfs_path(path)
    assert_safe_sysfs_value(value)
    if path not in allowed_paths:
        raise RuntimeError(
            "A escrita foi recusada: o caminho não apareceu como nó legível na descoberta. "
            "Use --discover-only, confirme o nó e tente novamente."
        )

    before = read_remote_file(runner, path)
    remote_command = f"printf '%s' {shlex.quote(value)} > {shlex.quote(path)}"
    write_result = runner.run(["shell", "sh", "-c", remote_command], timeout=10)
    after = read_remote_file(runner, path)
    write_json(
        output_dir / "vendor_sysfs_write_transaction.json",
        {
            "timestamp_utc": utc_now(),
            "path": path,
            "requested_value": value,
            "before": before.to_json(),
            "write": write_result.to_json(),
            "after": after.to_json(),
        },
    )
    if write_result.returncode != 0:
        raise RuntimeError(
            "A escrita foi recusada pelo aparelho. Consulte vendor_sysfs_write_transaction.json. "
            "Não tente outro nó automaticamente."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_adb_arguments(parser)
    parser.add_argument("--duration", type=float, default=60.0, help="Duração da vigilância em segundos.")
    parser.add_argument("--interval", type=float, default=0.25, help="Intervalo entre leituras sysfs em segundos.")
    parser.add_argument(
        "--snapshot-every",
        type=float,
        default=5.0,
        help="Intervalo dos snapshots de sensorservice/power em segundos.",
    )
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="Caminho sysfs adicional a monitorar; pode ser informado mais de uma vez.",
    )
    parser.add_argument("--discover-only", action="store_true", help="Apenas gera a descoberta e sai.")
    parser.add_argument(
        "--write",
        nargs=2,
        metavar=("PATH", "VALUE"),
        help="Escrita única em sysfs de fornecedor. Nunca ocorre por padrão.",
    )
    parser.add_argument(
        "--confirm-vendor-sysfs-write",
        action="store_true",
        help="Confirma que o nó e o efeito são de responsabilidade do fornecedor/engenharia.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.duration <= 0 or args.interval <= 0 or args.snapshot_every <= 0:
        raise SystemExit("--duration, --interval e --snapshot-every devem ser maiores que zero.")
    if args.write and not args.confirm_vendor_sysfs_write:
        raise SystemExit(
            "Para escrever em sysfs, adicione --confirm-vendor-sysfs-write. "
            "AOSP não define esse nó nem seus valores."
        )

    runner = runner_from_args(args)
    output_dir = create_output_dir(args.out, "proximity", runner.serial)
    services = dump_services(runner, output_dir)
    capture_framework_snapshot(runner, output_dir, services, index=0)

    candidates = discover_sysfs_candidates(runner)
    write_json(output_dir / "sysfs_candidates.json", [asdict(item) for item in candidates])
    auto_paths = [path for item in candidates for path in item.value_paths]
    for path in args.path:
        assert_safe_sysfs_path(path)
    selected_paths = list(dict.fromkeys([*args.path, *auto_paths]))
    allowed_paths = set(auto_paths)

    write_text(
        output_dir / "README.txt",
        "AOSP: dumpsys sensorservice/power. Vendor: sysfs_candidates.json. "
        "Os valores sysfs não são semanticamente equivalentes aos centímetros do Sensor HAL.\n",
    )

    if args.write:
        guarded_write(runner, output_dir, args.write[0], args.write[1], allowed_paths)
        print("Transação de escrita concluída; confira o before/after no JSON.")

    if args.discover_only:
        print(f"Descoberta salva em: {output_dir}")
        return 0
    if not selected_paths:
        print("Nenhum nó sysfs de valor foi encontrado. Os dumps AOSP foram salvos para análise.")
        print(f"Saída: {output_dir}")
        return 0

    csv_path = output_dir / "proximity_samples.csv"
    print(f"Monitorando {len(selected_paths)} nó(s) por {args.duration:.1f}s em {output_dir}")
    start = time.monotonic()
    next_sample = start
    next_snapshot = start + args.snapshot_every
    snapshot_index = 1
    try:
        with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(
                csv_file, fieldnames=["timestamp_utc", "elapsed_s", "path", "returncode", "value_or_error"]
            )
            writer.writeheader()
            while time.monotonic() - start < args.duration:
                now = time.monotonic()
                if now >= next_sample:
                    for path in selected_paths:
                        result = read_remote_file(runner, path)
                        value = result.stdout.strip() if result.returncode == 0 else result.stderr.strip()
                        writer.writerow(
                            {
                                "timestamp_utc": utc_now(),
                                "elapsed_s": f"{now - start:.3f}",
                                "path": path,
                                "returncode": result.returncode,
                                "value_or_error": value,
                            }
                        )
                    csv_file.flush()
                    next_sample += args.interval
                if now >= next_snapshot:
                    capture_framework_snapshot(runner, output_dir, services, index=snapshot_index)
                    snapshot_index += 1
                    next_snapshot += args.snapshot_every
                time.sleep(min(0.05, max(0.001, next_sample - time.monotonic())))
    except KeyboardInterrupt:
        print("Monitoramento interrompido pelo usuário; preservando as amostras coletadas.")
    finally:
        capture_framework_snapshot(runner, output_dir, services, index=snapshot_index)
    write_json(
        output_dir / "monitor.manifest.json",
        {
            "finished_at_utc": utc_now(),
            "serial": runner.serial,
            "selected_paths": selected_paths,
            "duration_seconds": args.duration,
            "interval_seconds": args.interval,
        },
    )
    print(f"Amostras salvas em: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
