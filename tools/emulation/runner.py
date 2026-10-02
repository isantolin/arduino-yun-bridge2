#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] McuBridge Emulation & Fuzzing Orchestration Runner."""

from __future__ import annotations

from pathlib import Path
import subprocess
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
        compile_script = REPO_ROOT / "tools" / "ci" / "compile_emulator.sh"
        emulator_bin = REPO_ROOT / "mcubridge-library-arduino" / "tests" / "bridge_control_emulator"
        if not emulator_bin.exists() and compile_script.exists():
            subprocess.run(["bash", str(compile_script)], cwd=str(REPO_ROOT), check=True)
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

            from tools.emulation.protocol_fuzzer import main as run_fuzzer

            run_fuzzer(port=str(fuzz_pty), count=fuzz_iterations)
        finally:
            terminate_process_tree([proc], timeout=2.0)
            fuzz_pty.unlink(missing_ok=True)
        return

    from tools.emulation.simavr_runner import main as run_simavr

    run_simavr(board=fqbn)


if __name__ == "__main__":
    cli()
