#!/usr/bin/env python3
"""Observa uma autenticação biométrica legítima sem injetar dedo/match."""

from __future__ import annotations

import argparse

from common_adb import add_adb_arguments, create_output_dir, runner_from_args
from probe_utils import run_observational_scenario


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_adb_arguments(parser)
    parser.add_argument("--duration", type=float, default=45.0, help="Janela de observação em segundos.")
    parser.add_argument(
        "--flow",
        choices=("authenticate", "detect-interaction"),
        default="authenticate",
        help="Apenas descreve o fluxo que o operador deverá iniciar; não o dispara.",
    )
    parser.add_argument(
        "--filter-spec",
        help="Filter-spec oficial do logcat, se tags do build já forem conhecidas.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.duration <= 0:
        raise SystemExit("--duration deve ser maior que zero.")
    runner = runner_from_args(args)
    output_dir = create_output_dir(args.out, "fingerprint_probe", runner.serial)
    if args.flow == "authenticate":
        step = (
            "Abra a tela de bloqueio ou um app de teste autorizado que já chame BiometricPrompt, "
            "então toque um dedo cadastrado e, em seguida, um dedo não cadastrado se o procedimento permitir."
        )
    else:
        step = (
            "Inicie no seu app de engenharia um fluxo real de detectInteraction e toque o sensor. "
            "Não use propriedades de virtual HAL em um telefone com HAL de produção."
        )
    run_observational_scenario(
        runner=runner,
        output_dir=output_dir,
        category="fingerprint",
        duration=args.duration,
        relevant_services=("fingerprint", "biometric"),
        filter_spec=args.filter_spec,
        instructions=f"""
            {step}

            Esta ferramenta somente observa dumps e logcat. Um match/fail genuíno é decidido
            pelo HAL/TEE; ADB não possui uma interface pública para fabricar um dedo ou um
            resultado de autenticação em um sensor físico.
        """,
    )
    print(f"Coleta fingerprint concluída: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
