#!/usr/bin/env python3
"""Coleta evidência de display/power durante flicker, green line ou perda de painel."""

from __future__ import annotations

import argparse

from common_adb import add_adb_arguments, create_output_dir, runner_from_args
from probe_utils import run_observational_scenario


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_adb_arguments(parser)
    parser.add_argument("--duration", type=float, default=60.0, help="Janela de observação em segundos.")
    parser.add_argument(
        "--filter-spec",
        help="Filter-spec oficial do logcat. Sem ele, um filtro local amplo preserva o raw completo.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.duration <= 0:
        raise SystemExit("--duration deve ser maior que zero.")
    runner = runner_from_args(args)
    output_dir = create_output_dir(args.out, "display_probe", runner.serial)
    run_observational_scenario(
        runner=runner,
        output_dir=output_dir,
        category="display",
        duration=args.duration,
        relevant_services=("display", "SurfaceFlinger", "power", "sensorservice"),
        filter_spec=args.filter_spec,
        instructions="""
            Reproduza o flicker/linha verde/perda de imagem sem alterar o telefone durante a
            coleta. Para o cenário de chamada, aproxime e afaste o rosto do sensor de
            proximidade. O raw logcat preserva palavras de kernel/OEM como I2C, DSI, MIPI,
            DRM e panel quando elas chegam ao logd; AOSP não possui um dump genérico que
            represente o barramento I2C de todo fornecedor.
        """,
    )
    print(f"Coleta display concluída: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
