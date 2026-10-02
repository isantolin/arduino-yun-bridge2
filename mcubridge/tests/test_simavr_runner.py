"""Unit tests for tools/emulation/simavr_runner.py (SIL-2 / Rule 11 / Rule 18 / Rule 37)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.emulation import simavr_runner


def test_simavr_state_on_line() -> None:
    state = simavr_runner.SimavrState()
    assert not state.sync_event.is_set()

    state.on_line("Handshake synchronization complete", "simavr")
    assert state.sync_event.is_set()

    state.on_line("MCU capabilities received", "simavr")
    assert state.capabilities_event.is_set()


def test_run_single_client_script_with_main(tmp_path: Path) -> None:
    script = tmp_path / "test_client.py"
    script.write_text(
        "executed_args = {}\n"
        "def main(host=None, port=None, device_id=None):\n"
        "    global executed_args\n"
        "    executed_args['device_id'] = device_id\n",
        encoding="utf-8",
    )
    passed = simavr_runner._run_single_client_script(script, device_id="test-mcu-01")
    assert passed is True


def test_run_single_client_script_with_exit_code(tmp_path: Path) -> None:
    script_pass = tmp_path / "pass_exit.py"
    script_pass.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    assert simavr_runner._run_single_client_script(script_pass) is True

    script_fail = tmp_path / "fail_exit.py"
    script_fail.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    assert simavr_runner._run_single_client_script(script_fail) is False


def test_run_single_client_script_exception(tmp_path: Path) -> None:
    script_err = tmp_path / "error_script.py"
    script_err.write_text(
        "def main(host=None, port=None, device_id=None):\n    raise RuntimeError('boom')\n",
        encoding="utf-8",
    )
    assert simavr_runner._run_single_client_script(script_err) is False


def test_run_client_scripts(tmp_path: Path) -> None:
    s1 = tmp_path / "s1.py"
    s1.write_text("def main(host=None, port=None, device_id=None):\n    pass\n", encoding="utf-8")
    missing = tmp_path / "does_not_exist.py"

    passed = simavr_runner._run_client_scripts([s1, missing], {}, 10.0)
    assert passed is True


def test_run_matrix_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sketch = tmp_path / "TestSketch.ino"
    sketch.write_text("// test sketch\n", encoding="utf-8")

    def mock_compile_run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        _ = check
        out_dir = Path(cmd[4])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "firmware.elf").write_text("ELF_BINARY", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_compile_run)
    monkeypatch.setattr(simavr_runner, "run_simavr_emulation", lambda **kwargs: True)
    monkeypatch.setenv("SIMAVR_METRICS_DIR", str(tmp_path / "metrics"))

    fail_count = simavr_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 0

    summary_file = tmp_path / "metrics" / "simavr_summary.md"
    assert summary_file.exists()
    content = summary_file.read_text(encoding="utf-8")
    assert "Passed (100% E2E)" in content
    assert "Arduino Mega 2560" in content


def test_run_matrix_compilation_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sketch = tmp_path / "TestSketch.ino"
    sketch.write_text("// test sketch\n", encoding="utf-8")

    def mock_compile_skip(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        _ = check
        out_dir = Path(cmd[4])
        out_dir.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_compile_skip)
    monkeypatch.setattr(simavr_runner, "run_simavr_emulation", lambda **kwargs: True)
    monkeypatch.setenv("SIMAVR_METRICS_DIR", str(tmp_path / "metrics"))

    fail_count = simavr_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 0

    summary_file = tmp_path / "metrics" / "simavr_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "SKIPPED" in content


def test_run_matrix_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sketch = tmp_path / "TestSketch.ino"
    sketch.write_text("// test sketch\n", encoding="utf-8")

    def mock_compile_run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        _ = check
        out_dir = Path(cmd[4])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "firmware.elf").write_text("ELF_BINARY", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_compile_run)
    monkeypatch.setattr(simavr_runner, "run_simavr_emulation", lambda **kwargs: False)
    monkeypatch.setenv("SIMAVR_METRICS_DIR", str(tmp_path / "metrics"))

    fail_count = simavr_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 3  # 3 boards in matrix failed

    summary_file = tmp_path / "metrics" / "simavr_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "FAIL" in content
