#!/usr/bin/env python3
"""Coleta um snapshot não invasivo de serviços Android relevantes para hardware.

O script não limpa logcat, não reinicia serviços, não executa ``adb root`` e não
escreve no aparelho. Ele registra o build e somente serviços que o próprio
``dumpsys -l`` anuncia, para funcionar entre Android 15, 16, 17 e OEMs.
"""

from __future__ import annotations

import argparse

from common_adb import (
    add_adb_arguments,
    capture_dumpsys_if_present,
    create_output_dir,
    dump_services,
    run_and_save,
    runner_from_args,
    utc_now,
    write_json,
    write_text,
)


TARGET_SERVICES = (
    "sensorservice",
    "power",
    "display",
    "SurfaceFlinger",
    "nfc",
    "fingerprint",
    "biometric",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_adb_arguments(parser)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    runner = runner_from_args(args)
    output_dir = create_output_dir(args.out, "android_snapshot", runner.serial)

    # Identidade do host/target e versão de cada ferramenta consultada.
    run_and_save(runner, output_dir, "adb_version", ["version"])
    run_and_save(runner, output_dir, "adb_devices", ["devices", "-l"])
    run_and_save(runner, output_dir, "getprop_all", ["shell", "getprop"])
    run_and_save(runner, output_dir, "logcat_help", ["logcat", "--help"])

    services = dump_services(runner, output_dir)
    captured: list[str] = []
    skipped: list[str] = []
    for service in TARGET_SERVICES:
        result = capture_dumpsys_if_present(runner, output_dir, services, service, timeout=45)
        if result is None:
            skipped.append(service)
        else:
            captured.append(service)

    # Estes três caminhos não são AOSP: são apenas locais comuns de drivers
    # Linux de fabricantes. Falhas (diretório ausente/SELinux) são salvas.
    for label, path in (
        ("sysfs_class_sensors", "/sys/class/sensors"),
        ("sysfs_iio_devices", "/sys/bus/iio/devices"),
        ("sysfs_virtual_sensors", "/sys/devices/virtual/sensors"),
    ):
        run_and_save(runner, output_dir, label, ["shell", "ls", "-la", path])

    write_json(
        output_dir / "snapshot.manifest.json",
        {
            "captured_at_utc": utc_now(),
            "serial": runner.serial,
            "captured_dumpsys_services": captured,
            "not_advertised_dumpsys_services": skipped,
            "read_only": True,
        },
    )
    write_text(
        output_dir / "README.txt",
        "Snapshot não invasivo. A saída completa de cada comando e seu código de retorno foram preservados.\n",
    )
    print(f"Snapshot salvo em: {output_dir}")
    print("Serviços capturados:", ", ".join(captured) or "nenhum")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
