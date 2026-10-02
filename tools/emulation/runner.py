#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] McuBridge Emulation & Fuzzing Orchestration Runner."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Annotated
import typer

from tools.emulation.process_utils import terminate_process_tree, wait_for_path_ready

cli = typer.Typer(
    help="[MIL-SPEC/SIL-2] McuBridge Emulation & Fuzzing Runner",
    add_completion=False,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@cli.command()
def main(
    fqbn: Annotated[str, typer.Option("--fqbn", help="Target MCU FQBN")] = "arduino:avr:uno",
    fuzz: Annotated[bool, typer.Option("--fuzz", help="Execute protocol fuzzing campaign")] = False,
    fuzz_iterations: Annotated[int, typer.Option("--fuzz-iterations", help="Number of fuzzing iterations")] = 500,
) -> None:
    """Run MCU bridge emulation or protocol fuzzing suite."""
    if fuzz:
        _ = fqbn
        fuzz_script = REPO_ROOT / "tools" / "ci" / "ci_fuzz.sh"
        if fuzz_script.exists():
            env: dict[str, str] = dict(os.environ)
            env["PYTHONPATH"] = f"{REPO_ROOT}:{REPO_ROOT / 'mcubridge'}:{env.get('PYTHONPATH', '')}"
            res_sh: subprocess.CompletedProcess[bytes] = subprocess.run(
                ["bash", str(fuzz_script)], env=env, cwd=str(REPO_ROOT), check=False
            )
            if res_sh.returncode != 0:
                sys.exit(res_sh.returncode)
            return

        compile_script = REPO_ROOT / "tools" / "ci" / "compile_emulator.sh"
        if compile_script.exists():
            subprocess.run(["bash", str(compile_script)], cwd=str(REPO_ROOT), check=True)

        emulator_bin = REPO_ROOT / "mcubridge-library-arduino" / "tests" / "bridge_control_emulator"
        fuzz_pty = Path("/tmp/ttyBRIDGE_FUZZ")
        fuzz_pty.unlink(missing_ok=True)

        socat_cmd = [
            "socat",
            "-d",
            "-d",
            f"PTY,link={fuzz_pty},raw,echo=0",
            f'EXEC:"{emulator_bin}",pty,raw,echo=0',
        ]
        proc: subprocess.Popen[bytes] = subprocess.Popen(
            socat_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(REPO_ROOT),
        )
        try:
            if not wait_for_path_ready(fuzz_pty, timeout=4.0, interval=0.2):
                raise RuntimeError("PTY device never appeared")

            env = dict(os.environ)
            env["PYTHONPATH"] = f"{REPO_ROOT}:{REPO_ROOT / 'mcubridge'}:{env.get('PYTHONPATH', '')}"
            fuzzer_cmd = [
                sys.executable,
                str(REPO_ROOT / "tools" / "emulation" / "protocol_fuzzer.py"),
                "--port",
                str(fuzz_pty),
                "--count",
                str(fuzz_iterations),
            ]
            res_fuzz: subprocess.CompletedProcess[bytes] = subprocess.run(
                fuzzer_cmd, env=env, cwd=str(REPO_ROOT), check=False
            )
            if res_fuzz.returncode != 0:
                sys.exit(res_fuzz.returncode)
        finally:
            terminate_process_tree([proc], timeout=2.0)
            fuzz_pty.unlink(missing_ok=True)
        return

    simavr_script = REPO_ROOT / "tools" / "emulation" / "simavr_runner.py"
    res_mcu: subprocess.CompletedProcess[bytes] = subprocess.run(
        [sys.executable, str(simavr_script), "--board", fqbn],
        cwd=str(REPO_ROOT),
        check=False,
    )
    if res_mcu.returncode != 0:
        sys.exit(res_mcu.returncode)


if __name__ == "__main__":
    cli()
