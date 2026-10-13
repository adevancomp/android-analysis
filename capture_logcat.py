#!/usr/bin/env python3
"""Captura logcat de forma reproduzível, preservando o log bruto e um recorte.

Não limpa buffers, não muda propriedades ``log.tag.*`` e não faz root. Assim, a
coleta é observacional e pode ser usada durante um cenário já existente.
"""

from __future__ import annotations

import argparse
import queue
import re
import threading
import time
from pathlib import Path
from typing import Pattern

from common_adb import (
    AdbRunner,
    add_adb_arguments,
    create_output_dir,
    run_and_save,
    runner_from_args,
    utc_now,
    write_json,
    write_text,
)


# Os padrões são deliberadamente amplos: OEMs trocam as tags de driver/HAL.
# O arquivo raw permanece a fonte de verdade quando uma tag não estiver aqui.
CATEGORY_PATTERNS: dict[str, str] = {
    "nfc": r"(?i)\b(?:nfc|nci|nfcc|libnfc|nxpnci|st21nfc|nfc_hal|ndef|iso[- ]?dep)\b",
    "fingerprint": r"(?i)\b(?:fingerprint|biometric|biometrics|udfps|goodix|fpc|egis|fp_hal)\b",
    "proximity": r"(?i)\b(?:proximity|prox(?:_sensor)?|sensorservice|displaypower|powermanager)\b",
    "display": r"(?i)\b(?:surfaceflinger|displaypower|displaymanager|hwcomposer|composer|drm|dsi|mipi|panel|i2c|flicker)\b",
}


class LogcatCapture:
    """Executa ``adb logcat`` e grava em thread para não bloquear o tempo limite."""

    def __init__(
        self,
        runner: AdbRunner,
        output_dir: Path,
        *,
        category: str = "all",
        extra_match: str | None = None,
        filter_spec: str | None = None,
    ) -> None:
        self.runner = runner
        self.output_dir = output_dir
        self.category = category
        self.filter_spec = filter_spec
        pattern_text = CATEGORY_PATTERNS.get(category, "")
        if extra_match:
            pattern_text = f"(?:{pattern_text})|(?:{extra_match})" if pattern_text else extra_match
        self.pattern: Pattern[str] | None = re.compile(pattern_text) if pattern_text else None
        self.process = None
        self.lines: queue.Queue[str | None] = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self.total_lines = 0
        self.matched_lines = 0

    def start(self) -> None:
        # A documentação do Android recomenda consultar --help do aparelho,
        # pois algumas opções dependem da versão. O resultado também é salvo.
        run_and_save(self.runner, self.output_dir, "logcat_help", ["logcat", "--help"])
        args = ["logcat", "-b", "all", "-v", "threadtime"]
        if self.filter_spec:
            # Uma única string é o formato oficial de filter-spec do logcat.
            args.append(self.filter_spec)
        self.process = self.runner.popen(args)
        self._reader_thread = threading.Thread(target=self._reader, daemon=True)
        self._reader_thread.start()

    def _reader(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def drain_once(self, timeout: float = 0.20) -> bool:
        """Grava uma linha disponível; retorna falso quando o processo terminou."""

        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            return self.process is not None and self.process.poll() is None
        if line is None:
            return False

        raw_path = self.output_dir / "logcat.raw.txt"
        with raw_path.open("a", encoding="utf-8", errors="replace") as raw_file:
            raw_file.write(line)
        self.total_lines += 1
        if self.pattern is None or self.pattern.search(line):
            with (self.output_dir / "logcat.filtered.txt").open(
                "a", encoding="utf-8", errors="replace"
            ) as filtered_file:
                filtered_file.write(line)
            self.matched_lines += 1
        return True

    def run_for(self, duration_seconds: float) -> None:
        end = time.monotonic() + duration_seconds
        while time.monotonic() < end:
            if not self.drain_once(timeout=min(0.20, max(0.01, end - time.monotonic()))):
                break

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except Exception:
                self.process.kill()
                self.process.wait(timeout=3)
        # Esvazia as poucas linhas emitidas antes de o processo encerrar.
        while self.drain_once(timeout=0.01):
            pass

    def write_manifest(self) -> None:
        write_json(
            self.output_dir / "logcat.capture.json",
            {
                "captured_at_utc": utc_now(),
                "serial": self.runner.serial,
                "category": self.category,
                "filter_spec": self.filter_spec,
                "lines_total": self.total_lines,
                "lines_matched": self.matched_lines,
                "device_command": self.runner.argv("logcat", "-b", "all", "-v", "threadtime"),
            },
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_adb_arguments(parser)
    parser.add_argument(
        "--category",
        choices=["all", *CATEGORY_PATTERNS],
        default="all",
        help="Recorte local por assunto; o arquivo raw sempre é salvo inteiro.",
    )
    parser.add_argument("--duration", type=float, default=60.0, help="Duração em segundos.")
    parser.add_argument(
        "--match",
        help="Expressão regular adicional aplicada localmente (não é enviada ao aparelho).",
    )
    parser.add_argument(
        "--filter-spec",
        help=(
            "Filter expression oficial do logcat, por exemplo: "
            "'NfcService:D NfcDispatcher:D *:S'. Use com cautela: linhas excluídas aqui "
            "não aparecerão nem no raw."
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.duration <= 0:
        raise SystemExit("--duration deve ser maior que zero.")
    runner = runner_from_args(args)
    output_dir = create_output_dir(args.out, f"logcat_{args.category}", runner.serial)
    write_text(output_dir / "README.txt", "Coleta observacional de logcat; os buffers não foram limpos.\n")
    capture = LogcatCapture(
        runner,
        output_dir,
        category=args.category,
        extra_match=args.match,
        filter_spec=args.filter_spec,
    )
    try:
        capture.start()
        print(f"Capturando por {args.duration:.1f}s em {output_dir}")
        capture.run_for(args.duration)
    except KeyboardInterrupt:
        print("Interrompido pelo usuário; preservando o que já foi coletado.")
    finally:
        capture.stop()
        capture.write_manifest()
    print(f"Linhas: {capture.total_lines}; selecionadas: {capture.matched_lines}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
