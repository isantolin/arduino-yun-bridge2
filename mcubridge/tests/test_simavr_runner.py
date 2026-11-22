"""Unit and pw_emu integration tests for tools/emulation/simavr_runner.py."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import typer
from pw_emu.core import Config as PwEmuConfig
from pw_emu.core import Launcher, NotRunning
from pw_emu.frontend import Emulator
from pw_emu.qemu import QemuLauncher
from pw_emu.renode import RenodeLauncher
from pw_emu_avr.simavr import SimavrLauncher

from tools.arduino_core_metadata import ArduinoCoreMetadata, write_metadata_json
from tools.emulation import simavr_runner, test_fleet_mesh_hardware_e2e


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


def test_pw_emu_config_registers_all_backends_and_avr_targets() -> None:
    cfg_path = simavr_runner.pw_emu_config_path()
    assert cfg_path.is_file()
    cfg = PwEmuConfig(cfg_path, target="atmega2560", emu="simavr")
    assert "atmega2560" in cfg.get_targets()
    assert "atmega328p" in cfg.get_targets()
    assert isinstance(Launcher.get("simavr", cfg_path), SimavrLauncher)
    assert isinstance(Launcher.get("qemu", cfg_path), QemuLauncher)
    assert isinstance(Launcher.get("renode", cfg_path), RenodeLauncher)
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert raw["pw"]["pw_emu"]["simavr"]["channels"]["gdb"]["enabled"] is False


def test_start_and_stop_simavr_emulator_delegates_to_pw_emu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, Any] = {}

    class _FakeEmulator:
        def __init__(self, wdir: Path, config_path: Path | None = None) -> None:
            recorded["wdir"] = wdir
            recorded["config_path"] = config_path
            self.stopped = False

        def start(
            self,
            target: str,
            file: Path | None = None,
            pause: bool = False,
            args: str | None = None,
        ) -> None:
            recorded["start"] = (target, file, pause, args)

        def get_channel_path(self, name: str) -> str:
            recorded["channel"] = name
            return "/dev/pts/42"

        def stop(self) -> None:
            self.stopped = True
            raise NotRunning(recorded["wdir"])

    monkeypatch.setattr(simavr_runner, "Emulator", _FakeEmulator)

    fw = tmp_path / "firmware.elf"
    fw.write_bytes(b"ELF")
    wdir = tmp_path / "wdir"
    emu, pty_path = simavr_runner.start_simavr_emulator(wdir, fw, "atmega2560", 16_000_000, "1", pause=True)
    assert pty_path == "/dev/pts/42"
    assert recorded["wdir"] == wdir
    assert recorded["config_path"] == simavr_runner.pw_emu_config_path()
    assert recorded["start"] == ("atmega2560", fw, True, "--frequency 16000000")
    assert recorded["channel"] == "uart1"

    # stop_simavr_emulator tolerates NotRunning without raising.
    simavr_runner.stop_simavr_emulator(emu)
    simavr_runner.stop_simavr_emulator(None)


def test_report_simavr_diagnostics_writes_watermark_and_handles_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fw_dir = tmp_path / "arduino-avr-mega"
    fw_dir.mkdir()
    fw = fw_dir / "firmware.elf"
    fw.write_bytes(b"ELF")
    wdir = tmp_path / "pw_emu"
    wdir.mkdir()
    metrics_dir = tmp_path / "metrics"
    monkeypatch.setenv("SIMAVR_METRICS_DIR", str(metrics_dir))

    # Missing diagnostics.json returns None cleanly.
    assert simavr_runner.report_simavr_diagnostics(fw, wdir) is None

    # Valid watermark + crash writes canonical <target>_stack_watermark.json.
    (wdir / "simavr.log").write_text("avr_sadly_crashed\n", encoding="utf-8")
    (wdir / "diagnostics.json").write_text(
        json.dumps(
            {
                "min_sp_valid": True,
                "min_sp": 0x1E00,
                "ramend": 0x21FF,
                "peak_pc": 0x100,
                "address_size": 3,
                "peak_stack_hex": "",
                "crashed": True,
                "crash_pc": 0x200,
                "crash_sp": 0x1E00,
                "crash_sreg": 0x80,
                "crash_cycle": 12345,
                "crash_stack_hex": "",
            }
        ),
        encoding="utf-8",
    )

    sym = simavr_runner.report_simavr_diagnostics(fw, wdir)
    assert sym is not None
    assert sym["crashed"] is True
    assert sym["min_sp"] == 0x1E00

    watermark_file = metrics_dir / "arduino-avr-mega_stack_watermark.json"
    assert watermark_file.is_file()
    payload = json.loads(watermark_file.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["target"] == "arduino-avr-mega"
    assert payload["stack_used_bytes"] == 0x21FF - 0x1E00

    # Uninitialized min_sp (e.g. stopped while paused) does not crash or write bogus metrics.
    watermark_file.unlink()
    (wdir / "diagnostics.json").write_text(
        json.dumps(
            {
                "min_sp_valid": False,
                "min_sp": None,
                "ramend": 0x21FF,
                "peak_pc": 0,
                "address_size": 3,
                "peak_stack_hex": "",
                "crashed": False,
            }
        ),
        encoding="utf-8",
    )
    sym_uninit = simavr_runner.report_simavr_diagnostics(fw, wdir)
    assert sym_uninit is not None
    assert sym_uninit["min_sp"] is None
    assert not watermark_file.exists()


def test_fleet_watchdog_detects_exited_pw_emu_emulator(tmp_path: Path) -> None:
    emu = Emulator(tmp_path / "dead_emu", config_path=simavr_runner.pw_emu_config_path())
    reason = test_fleet_mesh_hardware_e2e.emulator_exit_reason("simavr", emu)
    assert reason == "emulator simavr exited during verification"


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
