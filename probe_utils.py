"""Funções compartilhadas pelos observadores NFC, fingerprint e display."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Sequence

from capture_logcat import LogcatCapture
from common_adb import (
    AdbRunner,
    capture_dumpsys_if_present,
    dump_services,
    utc_now,
    write_json,
    write_text,
)


def run_observational_scenario(
    *,
    runner: AdbRunner,
    output_dir: Path,
    category: str,
    duration: float,
    relevant_services: Sequence[str],
    instructions: str,
    filter_spec: str | None = None,
) -> None:
    """Captura antes/durante/depois sem acionar hardware, serviço ou app.

    A diferença é importante: este helper observa um estímulo físico ou fluxo
    legítimo iniciado pelo operador. Ele não usa broadcast, input, cmd oculto,
    propriedade de vendor ou injeção de Binder para fingir NFC/fingerprint.
    """

    services = dump_services(runner, output_dir)
    for service in relevant_services:
        capture_dumpsys_if_present(
            runner, output_dir, services, service, label=f"before_dumpsys_{service}", timeout=30
        )

    capture = LogcatCapture(
        runner,
        output_dir,
        category=category,
        filter_spec=filter_spec,
    )
    write_text(output_dir / "operator_instructions.txt", instructions.strip() + "\n")
    try:
        capture.start()
        print("\n" + instructions.strip() + "\n")
        print(f"Observando por {duration:.1f}s. Artefatos: {output_dir}")
        capture.run_for(duration)
    except KeyboardInterrupt:
        print("Interrompido pelo usuário; a coleta parcial será preservada.")
    finally:
        capture.stop()
        capture.write_manifest()

    # Dá poucos milissegundos para callbacks/serviços atualizarem seu dump.
    time.sleep(0.25)
    for service in relevant_services:
        capture_dumpsys_if_present(
            runner, output_dir, services, service, label=f"after_dumpsys_{service}", timeout=30
        )
    write_json(
        output_dir / "scenario.manifest.json",
        {
            "finished_at_utc": utc_now(),
            "serial": runner.serial,
            "category": category,
            "duration_seconds": duration,
            "advertised_services": sorted(services),
            "requested_service_snapshots": list(relevant_services),
            "read_only": True,
            "log_lines_total": capture.total_lines,
            "log_lines_matched": capture.matched_lines,
        },
    )
