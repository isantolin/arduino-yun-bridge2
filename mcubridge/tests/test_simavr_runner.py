"""Unit tests for tools/emulation/simavr_runner.py (SIL-2 / Rule 11 / Rule 18 / Rule 19 / Rule 37)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import typer

from tools.arduino_core_metadata import ArduinoCoreMetadata, write_metadata_json
from tools.emulation import simavr_runner


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
    monkeypatch.setattr(simavr_runner, "repo_root", tmp_path)
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
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    fail_count = simavr_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 0
    assert simavr_runner.MATRIX_BOARDS == [
        ("arduino:avr:mega", "Arduino Mega 2560 (ATmega2560)"),
    ]
    assert captured_metadata == [("core-selected-mcu", 8_000_000, 6)]

    summary_file = tmp_path / "metrics" / "simavr_summary.md"
    assert summary_file.exists()
    content = summary_file.read_text(encoding="utf-8")
    assert "Passed (100% E2E)" in content
    assert "Arduino Mega 2560" in content


def test_run_matrix_compilation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(simavr_runner, "repo_root", tmp_path)
    sketch = tmp_path / "TestSketch.ino"
    sketch.write_text("// test sketch\n", encoding="utf-8")

    def mock_compile_error(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        _ = check
        return subprocess.CompletedProcess(cmd, returncode=1)

    monkeypatch.setattr(subprocess, "run", mock_compile_error)
    monkeypatch.setattr(simavr_runner, "run_simavr_emulation", _mock_simavr_success)
    monkeypatch.setenv("SIMAVR_METRICS_DIR", str(tmp_path / "metrics"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    fail_count = simavr_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 1

    summary_file = tmp_path / "metrics" / "simavr_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "FAIL" in content
    assert "compilation failed" in content


def test_run_matrix_skipped_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(simavr_runner, "repo_root", tmp_path)
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
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    fail_count = simavr_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 0
    summary_file = tmp_path / "metrics" / "simavr_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "SKIPPED" in content
    assert "Flash/RAM limit exceeded" in content


def test_main_rejects_missing_sketch_without_substitution(tmp_path: Path) -> None:
    missing_sketch = tmp_path / "missing.ino"

    with pytest.raises(typer.BadParameter, match="Sketch does not exist"):
        simavr_runner.main(sketch=missing_sketch)


def test_run_matrix_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(simavr_runner, "repo_root", tmp_path)
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
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    fail_count = simavr_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 1

    summary_file = tmp_path / "metrics" / "simavr_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "FAIL" in content
