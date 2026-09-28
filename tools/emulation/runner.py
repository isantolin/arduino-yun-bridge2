#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] McuBridge Emulation & Fuzzing Orchestration Runner."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Annotated
import typer

cli = typer.Typer(help="[MIL-SPEC/SIL-2] McuBridge Emulation & Fuzzing Runner", add_completion=False)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@cli.command()
def main(
    fqbn: Annotated[str, typer.Option("--fqbn", help="Target MCU FQBN")] = "arduino:avr:uno",
    fuzz: Annotated[bool, typer.Option("--fuzz", help="Execute protocol fuzzing campaign")] = False,
    fuzz_iterations: Annotated[int, typer.Option("--fuzz-iterations", help="Number of fuzzing iterations")] = 500,
) -> None:
    """Run MCU bridge emulation or protocol fuzzing suite."""
    if fuzz:
        fuzz_script = REPO_ROOT / "tools" / "ci" / "ci_fuzz.sh"
        if fuzz_script.exists():
            env = dict(os.environ)
            env["PYTHONPATH"] = f"{REPO_ROOT}:{REPO_ROOT / 'mcubridge'}:{env.get('PYTHONPATH', '')}"
            res = subprocess.run(["bash", str(fuzz_script)], env=env, cwd=str(REPO_ROOT), check=False)
            if res.returncode != 0:
                sys.exit(res.returncode)
            return

        compile_script = REPO_ROOT / "tools" / "ci" / "compile_emulator.sh"
        if compile_script.exists():
            subprocess.run(["bash", str(compile_script)], cwd=str(REPO_ROOT), check=True)

        emulator_bin = REPO_ROOT / "mcubridge-library-arduino" / "tests" / "bridge_control_emulator"
        fuzz_pty = "/tmp/ttyBRIDGE_FUZZ"
        if os.path.exists(fuzz_pty):
            try:
                os.unlink(fuzz_pty)
            except OSError:
                pass

        socat_cmd = [
            "socat",
            "-d",
            "-d",
            f"PTY,link={fuzz_pty},raw,echo=0",
            f'EXEC:"{emulator_bin}",pty,raw,echo=0',
        ]
        proc = subprocess.Popen(socat_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(REPO_ROOT))
        try:
            for _ in range(20):
                if os.path.exists(fuzz_pty):
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("PTY device never appeared")

            env = dict(os.environ)
            env["PYTHONPATH"] = f"{REPO_ROOT}:{REPO_ROOT / 'mcubridge'}:{env.get('PYTHONPATH', '')}"
            fuzzer_cmd = [
                sys.executable,
                str(REPO_ROOT / "tools" / "emulation" / "protocol_fuzzer.py"),
                "--port",
                fuzz_pty,
                "--count",
                str(fuzz_iterations),
            ]
            res = subprocess.run(fuzzer_cmd, env=env, cwd=str(REPO_ROOT), check=False)
            if res.returncode != 0:
                sys.exit(res.returncode)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            if os.path.exists(fuzz_pty):
                try:
                    os.unlink(fuzz_pty)
                except OSError:
                    pass


if __name__ == "__main__":
    cli()
