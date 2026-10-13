#!/usr/bin/env python3
"""Injeta sensor somente no Android Emulator, usando o console documentado.

Este não é um substituto para a validação de driver/HAL em telefone físico. Ele
é útil para automatizar a camada framework/app com um AVD que tenha o sensor de
proximidade habilitado. A sintaxe/valores são confirmados pelo ``emu help
sensor`` do próprio Emulator antes de qualquer envio.
"""

from __future__ import annotations

import argparse

from common_adb import add_adb_arguments, create_output_dir, run_and_save, runner_from_args, write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_adb_arguments(parser)
    parser.add_argument("--sensor", required=True, help="Nome exatamente aceito por `adb emu help sensor`.")
    parser.add_argument(
        "--value",
        required=True,
        help="Formato exatamente aceito pelo console, confirmado na saída de `emu help sensor`.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Salva a ajuda, mas não envia o comando set.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    runner = runner_from_args(args)
    if not runner.serial.startswith("emulator-"):
        raise SystemExit(
            "Recusado: este comando é exclusivo do Android Emulator e não será enviado a um telefone físico."
        )
    output_dir = create_output_dir(args.out, "emulator_sensor", runner.serial)
    help_result = run_and_save(runner, output_dir, "emu_help_sensor", ["emu", "help", "sensor"])
    if help_result.returncode != 0:
        raise SystemExit("O console do Emulator não aceitou `help sensor`; consulte o arquivo salvo.")
    command = ["emu", "sensor", "set", args.sensor, args.value]
    if args.dry_run:
        write_json(output_dir / "emulator_sensor.manifest.json", {"dry_run": True, "command": runner.argv(*command)})
        print(f"Dry run salvo em: {output_dir}")
        return 0
    result = run_and_save(runner, output_dir, "emu_sensor_set", command)
    write_json(
        output_dir / "emulator_sensor.manifest.json",
        {"dry_run": False, "command": runner.argv(*command), "returncode": result.returncode},
    )
    if result.returncode != 0:
        raise SystemExit("O Emulator recusou o valor. Use a ajuda salva e ajuste apenas o AVD.")
    print(f"Evento enviado ao Emulator. Evidência salva em: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
