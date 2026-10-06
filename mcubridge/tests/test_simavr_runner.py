"""Unit tests for tools/emulation/simavr_runner.py (SIL-2 / Rule 11 / Rule 18 / Rule 37)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import typer

from tools.emulation import simavr_runner
from tools.arduino_core_metadata import ArduinoCoreMetadata, write_metadata_json


def _write_board_metadata(output_dir: Path, fqbn: str) -> None:
    write_metadata_json(
        ArduinoCoreMetadata(
            fqbn=fqbn,
            mcu="core-selected-mcu",
            frequency_hz=8_000_000,
            led_builtin=6,
            digital_pins=14,
            analog_inputs=6,
            spi_ss=10,
            spi_mosi=11,
            spi_miso=12,
            spi_sck=13,
            i2c_sda=18,
            i2c_scl=19,
        ),
        output_dir / "arduino_core_metadata.json",
    )


def test_simavr_state_on_line() -> None:
    state = simavr_runner.SimavrState()
    assert not state.sync_event.is_set()

    state.on_line("Handshake synchronization complete", "simavr")
    assert state.sync_event.is_set()

    state.on_line("MCU capabilities received", "simavr")
    assert state.capabilities_event.is_set()


def test_run_single_client_script_with_main(tmp_path: Path) -> None:
    script = tmp_path / "test_client.py"
    result_file = tmp_path / "led_pin.txt"
    script.write_text(
        "from pathlib import Path\n"
        "def main(host=None, port=None, device_id=None, led_builtin_pin=None):\n"
        f"    Path({str(result_file)!r}).write_text(str(led_builtin_pin), encoding='utf-8')\n",
        encoding="utf-8",
    )
    passed = simavr_runner.run_single_client_script(
        script,
        device_id="test-mcu-01",
        led_builtin_pin=9,
    )
    assert passed is True
    assert result_file.read_text(encoding="utf-8") == "9"


def test_run_single_client_script_with_exit_code(tmp_path: Path) -> None:
    script_pass = tmp_path / "pass_exit.py"
    script_pass.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    assert simavr_runner.run_single_client_script(script_pass) is True

    script_fail = tmp_path / "fail_exit.py"
    script_fail.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    assert simavr_runner.run_single_client_script(script_fail) is False


def test_run_single_client_script_exception(tmp_path: Path) -> None:
    script_err = tmp_path / "error_script.py"
    script_err.write_text(
        "def main(host=None, port=None, device_id=None):\n    raise RuntimeError('boom')\n",
        encoding="utf-8",
    )
    assert simavr_runner.run_single_client_script(script_err) is False


def test_run_client_scripts(tmp_path: Path) -> None:
    s1 = tmp_path / "s1.py"
    s1.write_text("def main(host=None, port=None, device_id=None):\n    pass\n", encoding="utf-8")
    missing = tmp_path / "does_not_exist.py"

    passed = simavr_runner.run_client_scripts([s1, missing], {}, 10.0, led_builtin_pin=9)
    assert passed is True


def _mock_simavr_success(
    firmware_path: Path,
    mcu: str,
    frequency: int,
    test_scripts: list[Path],
    timeout_seconds: float = 90.0,
    uart_id: str | None = None,
    *,
    led_builtin_pin: int,
) -> bool:
    _ = (firmware_path, mcu, frequency, test_scripts, timeout_seconds, uart_id, led_builtin_pin)
    return True


def _mock_simavr_failure(
    firmware_path: Path,
    mcu: str,
    frequency: int,
    test_scripts: list[Path],
    timeout_seconds: float = 90.0,
    uart_id: str | None = None,
    *,
    led_builtin_pin: int,
) -> bool:
    _ = (firmware_path, mcu, frequency, test_scripts, timeout_seconds, uart_id, led_builtin_pin)
    return False


def test_run_matrix_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sketch = tmp_path / "TestSketch.ino"
    sketch.write_text("// test sketch\n", encoding="utf-8")

    def mock_compile_run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        _ = check
        out_dir = Path(cmd[4])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "firmware.elf").write_text("ELF_BINARY", encoding="utf-8")
        _write_board_metadata(out_dir, cmd[3])
        return subprocess.CompletedProcess(cmd, returncode=0)

    captured_metadata: list[tuple[str, int, int]] = []

    def mock_emulation(
        firmware_path: Path,
        mcu: str,
        frequency: int,
        test_scripts: list[Path],
        timeout_seconds: float = 90.0,
        uart_id: str | None = None,
        *,
        led_builtin_pin: int,
    ) -> bool:
        _ = (firmware_path, mcu, test_scripts, timeout_seconds, uart_id)
        captured_metadata.append((mcu, frequency, led_builtin_pin))
        return True

    monkeypatch.setattr(subprocess, "run", mock_compile_run)
    monkeypatch.setattr(simavr_runner, "run_simavr_emulation", mock_emulation)
    monkeypatch.setenv("SIMAVR_METRICS_DIR", str(tmp_path / "metrics"))

    fail_count = simavr_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 0
    assert captured_metadata == [("core-selected-mcu", 8_000_000, 6)] * 3

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
    monkeypatch.setattr(simavr_runner, "run_simavr_emulation", _mock_simavr_success)
    monkeypatch.setenv("SIMAVR_METRICS_DIR", str(tmp_path / "metrics"))

    fail_count = simavr_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 0

    summary_file = tmp_path / "metrics" / "simavr_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "SKIPPED" in content


def test_main_rejects_missing_sketch_without_substitution(tmp_path: Path) -> None:
    missing_sketch = tmp_path / "missing.ino"

    with pytest.raises(typer.BadParameter, match="Sketch does not exist"):
        simavr_runner.main(sketch=missing_sketch)


def test_run_matrix_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sketch = tmp_path / "TestSketch.ino"
    sketch.write_text("// test sketch\n", encoding="utf-8")

    def mock_compile_run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        _ = check
        out_dir = Path(cmd[4])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "firmware.elf").write_text("ELF_BINARY", encoding="utf-8")
        _write_board_metadata(out_dir, cmd[3])
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_compile_run)
    monkeypatch.setattr(simavr_runner, "run_simavr_emulation", _mock_simavr_failure)
    monkeypatch.setenv("SIMAVR_METRICS_DIR", str(tmp_path / "metrics"))

    fail_count = simavr_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 3  # 3 boards in matrix failed

    summary_file = tmp_path / "metrics" / "simavr_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "FAIL" in content
