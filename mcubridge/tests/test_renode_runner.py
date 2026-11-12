"""Unit tests for tools/emulation/renode_runner.py (SIL-2 / Rule 11 / Rule 18 / Rule 19 / Rule 37)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import typer

from tools.arduino_core_metadata import ArduinoCoreMetadata, write_metadata_json
from tools.emulation import renode_runner


def _write_board_metadata(output_dir: Path, fqbn: str) -> None:
    write_metadata_json(
        ArduinoCoreMetadata(
            fqbn=fqbn,
            mcu="cortex-m0plus",
            frequency_hz=48_000_000,
            led_builtin=6,
            digital_pins=14,
            analog_inputs=7,
            spi_ss=4,
            spi_mosi=8,
            spi_miso=10,
            spi_sck=9,
            i2c_sda=11,
            i2c_scl=12,
        ),
        output_dir / "arduino_core_metadata.json",
    )


def _mock_renode_success(
    firmware_path: Path,
    test_scripts: list[Path],
    timeout_seconds: float = 90.0,
    port: int = 12345,
    led_builtin_pin: int = 6,
    log_dir: Path | None = None,
    **kwargs: Any,
) -> bool:
    _ = (firmware_path, test_scripts, timeout_seconds, port, led_builtin_pin, log_dir, kwargs)
    return True


def _mock_renode_failure(
    firmware_path: Path,
    test_scripts: list[Path],
    timeout_seconds: float = 90.0,
    port: int = 12345,
    led_builtin_pin: int = 6,
    log_dir: Path | None = None,
    **kwargs: Any,
) -> bool:
    _ = (firmware_path, test_scripts, timeout_seconds, port, led_builtin_pin, log_dir, kwargs)
    return False


def _mock_which_renode_and_socat(cmd: str) -> str | None:
    return "/usr/bin/renode" if cmd == "renode" else "/usr/bin/socat"


def _mock_which_socat_only(cmd: str) -> str | None:
    return "/usr/bin/socat" if cmd == "socat" else None


def _mock_which_renode_only(cmd: str) -> str | None:
    return "/usr/bin/renode" if cmd == "renode" else None


def test_run_matrix_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renode_runner, "repo_root", tmp_path)
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
    monkeypatch.setattr(renode_runner, "run_renode_emulation", _mock_renode_success)
    monkeypatch.setattr(renode_runner.shutil, "which", _mock_which_renode_and_socat)
    monkeypatch.setenv("RENODE_METRICS_DIR", str(tmp_path / "metrics"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    fail_count = renode_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 0
    assert renode_runner.MATRIX_BOARDS == [("arduino:samd:mkrwifi1010", "Arduino MKR WiFi 1010 (ATSAMD21G18A)")]

    summary_file = tmp_path / "metrics" / "renode_summary.md"
    assert summary_file.exists()
    content = summary_file.read_text(encoding="utf-8")
    assert "Passed (100% E2E)" in content
    assert "Arduino MKR WiFi 1010" in content
    assert "PASS" in content


def test_run_matrix_renode_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renode_runner, "repo_root", tmp_path)
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
    monkeypatch.setattr(renode_runner.shutil, "which", _mock_which_socat_only)
    monkeypatch.setenv("RENODE_METRICS_DIR", str(tmp_path / "metrics"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    fail_count = renode_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 0

    summary_file = tmp_path / "metrics" / "renode_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "Skipped (Renode binary not installed)" in content
    assert "SKIPPED" in content


def test_run_matrix_compilation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renode_runner, "repo_root", tmp_path)
    sketch = tmp_path / "TestSketch.ino"
    sketch.write_text("// test sketch\n", encoding="utf-8")

    def mock_compile_error(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        _ = check
        return subprocess.CompletedProcess(cmd, returncode=1)

    monkeypatch.setattr(subprocess, "run", mock_compile_error)
    monkeypatch.setenv("RENODE_METRICS_DIR", str(tmp_path / "metrics"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    fail_count = renode_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 1

    summary_file = tmp_path / "metrics" / "renode_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "FAIL" in content
    assert "compilation failed" in content


def test_run_matrix_emulation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renode_runner, "repo_root", tmp_path)
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
    monkeypatch.setattr(renode_runner, "run_renode_emulation", _mock_renode_failure)
    monkeypatch.setattr(renode_runner.shutil, "which", _mock_which_renode_only)
    monkeypatch.setenv("RENODE_METRICS_DIR", str(tmp_path / "metrics"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    fail_count = renode_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 1

    summary_file = tmp_path / "metrics" / "renode_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "FAIL" in content


def test_run_renode_emulation_missing_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_elf = tmp_path / "firmware.elf"
    fake_elf.write_text("ELF", encoding="utf-8")

    monkeypatch.setattr(renode_runner.shutil, "which", _mock_which_socat_only)
    assert renode_runner.run_renode_emulation(fake_elf, []) is False

    monkeypatch.setattr(renode_runner.shutil, "which", _mock_which_renode_only)
    assert renode_runner.run_renode_emulation(fake_elf, []) is False


def test_main_rejects_missing_sketch(tmp_path: Path) -> None:
    missing_sketch = tmp_path / "missing.ino"
    with pytest.raises(typer.BadParameter, match="Sketch does not exist"):
        renode_runner.main(sketch=missing_sketch)
