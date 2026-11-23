"""Unit tests for tools/emulation/renode_runner.py (SIL-2 / Rule 11 / Rule 18 / Rule 19 / Rule 37)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import typer
from pw_emu.core import Error as PwEmuError

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
    led_builtin_pin: int = 6,
    log_dir: Path | None = None,
    **kwargs: Any,
) -> bool:
    _ = (firmware_path, test_scripts, timeout_seconds, led_builtin_pin, log_dir, kwargs)
    return True


def _mock_renode_failure(
    firmware_path: Path,
    test_scripts: list[Path],
    timeout_seconds: float = 90.0,
    led_builtin_pin: int = 6,
    log_dir: Path | None = None,
    **kwargs: Any,
) -> bool:
    _ = (firmware_path, test_scripts, timeout_seconds, led_builtin_pin, log_dir, kwargs)
    return False


def _mock_find_renode() -> str | None:
    return "/usr/local/bin/renode"


def _mock_find_renode_none() -> str | None:
    return None


def _mock_which_renode(cmd: str) -> str | None:
    return "/usr/bin/renode" if cmd == "renode" else None


def _mock_which_none(cmd: str) -> str | None:
    _ = cmd
    return None


def test_find_renode_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin_dir = tmp_path / "renode_dir"
    fake_bin_dir.mkdir(parents=True)
    fake_renode = fake_bin_dir / "renode"
    fake_renode.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_renode.chmod(0o755)

    monkeypatch.setattr(renode_runner.shutil, "which", _mock_which_renode)
    assert renode_runner.find_renode_binary() == "/usr/bin/renode"

    monkeypatch.setattr(renode_runner.shutil, "which", _mock_which_none)
    monkeypatch.setattr(renode_runner, "RENODE_SEARCH_DIRS", (fake_bin_dir,))
    assert renode_runner.find_renode_binary() == str(fake_renode)

    monkeypatch.setattr(renode_runner, "RENODE_SEARCH_DIRS", (tmp_path / "missing",))
    assert renode_runner.find_renode_binary() is None


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
    monkeypatch.setattr(renode_runner, "find_renode_binary", _mock_find_renode)
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
    monkeypatch.setattr(renode_runner, "find_renode_binary", _mock_find_renode_none)
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
    monkeypatch.setattr(renode_runner, "find_renode_binary", _mock_find_renode)
    monkeypatch.setenv("RENODE_METRICS_DIR", str(tmp_path / "metrics"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    fail_count = renode_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 1

    summary_file = tmp_path / "metrics" / "renode_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "FAIL" in content


def test_run_renode_emulation_missing_tools_and_firmware(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing_elf = tmp_path / "missing.elf"
    assert not renode_runner.run_renode_emulation(missing_elf, [])

    fake_elf = tmp_path / "firmware.elf"
    fake_elf.write_text("ELF", encoding="utf-8")
    monkeypatch.setattr(renode_runner, "find_renode_binary", _mock_find_renode_none)
    assert not renode_runner.run_renode_emulation(fake_elf, [])


def test_run_renode_emulation_full_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_elf = tmp_path / "firmware.elf"
    fake_elf.write_text("ELF", encoding="utf-8")
    log_dir = tmp_path / "logs"
    stopped: list[bool] = []

    class _FakeEmu:
        def stop(self) -> None:
            stopped.append(True)

    def mock_start_renode(wdir: Path, firmware_path: Path, *_args: Any, **_kwargs: Any) -> tuple[Any, str]:
        _ = firmware_path
        (wdir / "renode.log").write_text("Renode started via pw_emu\n", encoding="utf-8")
        return _FakeEmu(), str(wdir / "uart0")

    def mock_ensure_gateway(state: Any) -> subprocess.Popen[str] | None:
        _ = state
        return None

    def mock_tcp_ready(host: str, port: int, timeout: float = 30.0, interval: float = 0.5) -> bool:
        _ = (host, port, timeout, interval)
        return True

    def mock_setup_uci(slave: str, **kwargs: Any) -> tuple[Path, Path, dict[str, str]]:
        _ = (slave, kwargs)
        return (tmp_path / "uci", tmp_path / "db", {"FAKE": "1"})

    def mock_run_scripts(
        scripts: list[Path],
        *,
        device_id: str = "mkr-01",
        led_builtin_pin: int | None = 6,
    ) -> bool:
        _ = (scripts, device_id, led_builtin_pin)
        return True

    def mock_audit(status_file: Path = Path("/tmp/mcubridge_status.json")) -> bool:
        _ = status_file
        return True

    def mock_start_thread(*args: Any, **kwargs: Any) -> None:
        _ = (args, kwargs)

    def mock_terminate(procs: Any, timeout: float = 5.0) -> None:
        _ = (procs, timeout)

    class _MockPopen:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            _ = (args, kwargs)
            self.stdout = None
            self.stderr = None
            self.returncode = 0

        def poll(self) -> int | None:
            return None

    original_state_cls = renode_runner.EmulationState

    def mock_state() -> renode_runner.EmulationState:
        s = original_state_cls()
        s.sync_event.set()
        s.capabilities_event.set()
        return s

    monkeypatch.setattr(renode_runner, "find_renode_binary", _mock_find_renode)
    monkeypatch.setattr(renode_runner, "start_renode_emulator", mock_start_renode)
    monkeypatch.setattr(renode_runner, "ensure_cloud_gateway", mock_ensure_gateway)
    monkeypatch.setattr(renode_runner, "wait_for_tcp_ready", mock_tcp_ready)
    monkeypatch.setattr(renode_runner, "setup_fake_uci", mock_setup_uci)
    monkeypatch.setattr(renode_runner, "run_client_scripts", mock_run_scripts)
    monkeypatch.setattr(renode_runner, "audit_post_execution_status", mock_audit)
    monkeypatch.setattr(subprocess, "Popen", _MockPopen)
    monkeypatch.setattr(renode_runner, "start_daemon_thread", mock_start_thread)
    monkeypatch.setattr(renode_runner, "terminate_process_tree", mock_terminate)
    monkeypatch.setattr(renode_runner, "EmulationState", mock_state)

    passed = renode_runner.run_renode_emulation(
        firmware_path=fake_elf,
        test_scripts=[],
        timeout_seconds=5.0,
        log_dir=log_dir,
    )
    assert passed
    assert stopped == [True]
    exec_log = log_dir / "renode_execution.log"
    assert exec_log.exists()
    assert "Renode started via pw_emu" in exec_log.read_text(encoding="utf-8")

    # Failure to start pw_emu Renode returns False cleanly.
    def mock_start_error(*_args: Any, **_kwargs: Any) -> tuple[Any, str]:
        raise PwEmuError("robot server unavailable")

    monkeypatch.setattr(renode_runner, "start_renode_emulator", mock_start_error)
    assert not renode_runner.run_renode_emulation(firmware_path=fake_elf, test_scripts=[], timeout_seconds=5.0)


def test_main_rejects_missing_sketch(tmp_path: Path) -> None:
    missing_sketch = tmp_path / "missing.ino"
    with pytest.raises(typer.BadParameter, match="Sketch does not exist"):
        renode_runner.main(sketch=missing_sketch)
