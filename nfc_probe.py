#!/usr/bin/env python3
"""Observa um teste real de TAG NFC; não tenta gerar RF/NCI por ADB."""

from __future__ import annotations

import argparse

from common_adb import add_adb_arguments, create_output_dir, runner_from_args
from probe_utils import run_observational_scenario


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_adb_arguments(parser)
    parser.add_argument("--duration", type=float, default=45.0, help="Janela de observação em segundos.")
    parser.add_argument(
        "--filter-spec",
        help="Filter-spec oficial do logcat, se o build tiver tags AOSP/OEM já conhecidas.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.duration <= 0:
        raise SystemExit("--duration deve ser maior que zero.")
    runner = runner_from_args(args)
    output_dir = create_output_dir(args.out, "nfc_probe", runner.serial)
    run_observational_scenario(
        runner=runner,
        output_dir=output_dir,
        category="nfc",
        duration=args.duration,
        relevant_services=("nfc",),
        filter_spec=args.filter_spec,
        instructions="""
            Aproxime/remova uma TAG NFC física conhecida durante esta janela e reproduza
            exatamente o cenário de teste. Este script NÃO envia ACTION_NDEF_DISCOVERED,
            broadcasts, pacotes NCI nem comandos ao controlador: isso não validaria o
            caminho RF -> NFCC -> HAL -> NfcService.

            Compare before_dumpsys_nfc e after_dumpsys_nfc com logcat.raw.txt. Se a TAG
            não for detectada, preserve também a marcação temporal da aproximação.
        """,
    )
    print(f"Coleta NFC concluída: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
