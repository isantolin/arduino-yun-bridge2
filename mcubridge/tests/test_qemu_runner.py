"""Unit tests for tools/emulation/qemu_runner.py (SIL-2 / Rule 11 / Rule 18 / Rule 37)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import typer

from tools.arduino_core_metadata import ArduinoCoreMetadata, write_metadata_json
from tools.emulation import qemu_runner


def _write_board_metadata(output_dir: Path, fqbn: str, led_pin: int = 13) -> None:
    write_metadata_json(
        ArduinoCoreMetadata(
            fqbn=fqbn,
            mcu="esp32s3",
            frequency_hz=240_000_000,
            led_builtin=led_pin,
            digital_pins=49,
            analog_inputs=20,
            spi_ss=10,
            spi_mosi=11,
            spi_miso=13,
            spi_sck=12,
            i2c_sda=1,
            i2c_scl=2,
        ),
        output_dir / "arduino_core_metadata.json",
    )


def _mock_supports_true(path: str) -> bool:
    _ = path
    return True


def _mock_supports_false(path: str) -> bool:
    _ = path
    return False


def _mock_supports_usr_only(path: str) -> bool:
    return path == "/usr/bin/qemu-system-xtensa"


def _mock_which_qemu(cmd: str) -> str | None:
    _ = cmd
    return "/usr/bin/qemu-system-xtensa"


def _mock_which_none(cmd: str) -> str | None:
    _ = cmd
    return None


def _mock_which_usr(cmd: str) -> str | None:
    return f"/usr/bin/{cmd}"


def _mock_find_qemu() -> str | None:
    return "/usr/bin/qemu-system-xtensa"


def _mock_find_qemu_none() -> str | None:
    return None


def test_supports_esp32s3(monkeypatch: pytest.MonkeyPatch) -> None:
    def mock_run_supported(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        _ = (cmd, kwargs)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="Supported machines:\nesp32s3\nesp32")

    monkeypatch.setattr(subprocess, "run", mock_run_supported)
    assert qemu_runner.supports_esp32s3("/fake/qemu") is True

    def mock_run_unsupported(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        _ = (cmd, kwargs)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="Supported machines:\nsim\nqemu-xtensa")

    monkeypatch.setattr(subprocess, "run", mock_run_unsupported)
    assert qemu_runner.supports_esp32s3("/fake/qemu") is False

    def mock_run_error(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        _ = (cmd, kwargs)
        raise OSError("Executable failed")

    monkeypatch.setattr(subprocess, "run", mock_run_error)
    assert qemu_runner.supports_esp32s3("/fake/qemu") is False


def test_find_qemu_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir(parents=True)
    fake_qemu = fake_bin_dir / "qemu-system-xtensa"
    fake_qemu.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_qemu.chmod(0o755)

    monkeypatch.setattr(qemu_runner, "QEMU_SEARCH_DIRS", (fake_bin_dir,))
    monkeypatch.setattr(qemu_runner, "supports_esp32s3", _mock_supports_true)

    assert qemu_runner.find_qemu_binary() == str(fake_qemu)

    # If it does not support esp32s3, fallback to PATH
    monkeypatch.setattr(qemu_runner, "supports_esp32s3", _mock_supports_usr_only)
    monkeypatch.setattr(qemu_runner.shutil, "which", _mock_which_qemu)

    assert qemu_runner.find_qemu_binary() == "/usr/bin/qemu-system-xtensa"

    # If neither supports it, return None
    monkeypatch.setattr(qemu_runner, "supports_esp32s3", _mock_supports_false)
    assert qemu_runner.find_qemu_binary() is None


def test_build_qemu_command() -> None:
    flash_img = Path("/path/to/firmware_flash.bin")
    cmd = qemu_runner.build_qemu_command("/usr/bin/qemu-system-xtensa", flash_img, 12346)
    assert cmd == [
        "/usr/bin/qemu-system-xtensa",
        "-nographic",
        "-monitor",
        "none",
        "-machine",
        "esp32s3",
        "-drive",
        f"file={flash_img.resolve()},if=mtd,format=raw",
        "-serial",
        "tcp:127.0.0.1:12346,server,wait",
    ]


def test_qemu_state_on_line() -> None:
    state = qemu_runner.QemuState()
    assert not state.listening_event.is_set()
    assert not state.sync_event.is_set()

    state.on_line("QEMU waiting for connection on: 127.0.0.1:12346", "qemu")
    assert state.listening_event.is_set()

    state.on_line("Handshake synchronization complete", "daemon-stdout")
    assert state.sync_event.is_set()

    state.on_line("Capabilities discovery complete", "daemon-stdout")
    assert state.capabilities_event.is_set()


def test_audit_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    status_file = tmp_path / "mcubridge_status.json"

    def mock_path(p: str | Path) -> Path:
        return status_file if str(p) == "/tmp/mcubridge_status.json" else Path(p)

    monkeypatch.setattr(qemu_runner, "Path", mock_path)

    # File missing -> returns True
    assert qemu_runner.audit_status() is True

    # Valid status with 0 errors
    status_file.write_text(
        json.dumps(
            {
                "daemon": {"state": "CONNECTED"},
                "errors": [],
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )
    assert qemu_runner.audit_status() is True

    # Corrupt json -> returns False
    status_file.write_text("invalid json", encoding="utf-8")
    assert qemu_runner.audit_status() is False


def _mock_qemu_success(
    flash_image: Path,
    test_scripts: list[Path],
    timeout_seconds: float = 90.0,
    port: int = 12346,
    led_builtin_pin: int = 13,
    log_dir: Path | None = None,
    **kwargs: Any,
) -> bool:
    _ = (flash_image, test_scripts, timeout_seconds, port, led_builtin_pin, log_dir, kwargs)
    return True


def _mock_qemu_failure(
    flash_image: Path,
    test_scripts: list[Path],
    timeout_seconds: float = 90.0,
    port: int = 12346,
    led_builtin_pin: int = 13,
    log_dir: Path | None = None,
    **kwargs: Any,
) -> bool:
    _ = (flash_image, test_scripts, timeout_seconds, port, led_builtin_pin, log_dir, kwargs)
    return False


def test_run_matrix_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qemu_runner, "repo_root", tmp_path)
    sketch = tmp_path / "BridgeControl.ino"
    sketch.write_text("// test sketch\n", encoding="utf-8")

    def mock_compile_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        _ = (args, kwargs)
        out_dir = Path(cmd[4])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "firmware_flash.bin").write_text("FLASH_IMAGE", encoding="utf-8")
        _write_board_metadata(out_dir, cmd[3], led_pin=13)
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_compile_run)
    monkeypatch.setattr(qemu_runner, "find_qemu_binary", _mock_find_qemu)
    monkeypatch.setattr(qemu_runner, "run_qemu_emulation", _mock_qemu_success)
    monkeypatch.setenv("QEMU_METRICS_DIR", str(tmp_path / "metrics"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    fail_count = qemu_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 0
    assert qemu_runner.MATRIX_BOARDS == [("arduino:esp32:nano_nora", "Arduino Nano ESP32")]

    summary_file = tmp_path / "metrics" / "qemu_summary.md"
    assert summary_file.exists()
    content = summary_file.read_text(encoding="utf-8")
    assert "Passed (100% E2E)" in content
    assert "Arduino Nano ESP32" in content
    assert "PASS" in content

    json_file = tmp_path / "metrics" / "qemu_summary.json"
    assert json_file.exists()
    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert data["engine"] == "QEMU Espressif (Instruction-Accurate)"
    assert data["boards"][0]["result"] == "✅ PASS"


def test_run_matrix_qemu_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qemu_runner, "repo_root", tmp_path)
    sketch = tmp_path / "BridgeControl.ino"
    sketch.write_text("// test sketch\n", encoding="utf-8")

    def mock_compile_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        _ = (args, kwargs)
        out_dir = Path(cmd[4])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "firmware_flash.bin").write_text("FLASH_IMAGE", encoding="utf-8")
        _write_board_metadata(out_dir, cmd[3], led_pin=13)
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_compile_run)
    monkeypatch.setattr(qemu_runner, "find_qemu_binary", _mock_find_qemu_none)
    monkeypatch.setenv("QEMU_METRICS_DIR", str(tmp_path / "metrics"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    fail_count = qemu_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 0

    summary_file = tmp_path / "metrics" / "qemu_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "Skipped (QEMU binary not installed)" in content
    assert "SKIPPED" in content


def test_run_matrix_compilation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qemu_runner, "repo_root", tmp_path)
    sketch = tmp_path / "BridgeControl.ino"
    sketch.write_text("// test sketch\n", encoding="utf-8")

    def mock_compile_error(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        _ = (args, kwargs)
        return subprocess.CompletedProcess(cmd, returncode=1)

    monkeypatch.setattr(subprocess, "run", mock_compile_error)
    monkeypatch.setattr(qemu_runner, "find_qemu_binary", _mock_find_qemu)
    monkeypatch.setenv("QEMU_METRICS_DIR", str(tmp_path / "metrics"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    fail_count = qemu_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 1

    summary_file = tmp_path / "metrics" / "qemu_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "FAIL" in content
    assert "compilation failed" in content


def test_run_matrix_emulation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qemu_runner, "repo_root", tmp_path)
    sketch = tmp_path / "BridgeControl.ino"
    sketch.write_text("// test sketch\n", encoding="utf-8")

    def mock_compile_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        _ = (args, kwargs)
        out_dir = Path(cmd[4])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "firmware_flash.bin").write_text("FLASH_IMAGE", encoding="utf-8")
        _write_board_metadata(out_dir, cmd[3], led_pin=13)
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_compile_run)
    monkeypatch.setattr(qemu_runner, "find_qemu_binary", _mock_find_qemu)
    monkeypatch.setattr(qemu_runner, "run_qemu_emulation", _mock_qemu_failure)
    monkeypatch.setenv("QEMU_METRICS_DIR", str(tmp_path / "metrics"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    fail_count = qemu_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 1

    summary_file = tmp_path / "metrics" / "qemu_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "FAIL" in content


def test_run_qemu_emulation_missing_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_img = tmp_path / "firmware_flash.bin"
    fake_img.write_text("IMG", encoding="utf-8")

    # Missing qemu
    monkeypatch.setattr(qemu_runner, "find_qemu_binary", _mock_find_qemu_none)
    assert qemu_runner.run_qemu_emulation(fake_img, []) is False

    # Missing socat
    monkeypatch.setattr(qemu_runner, "find_qemu_binary", _mock_find_qemu)
    monkeypatch.setattr(qemu_runner.shutil, "which", _mock_which_none)
    assert qemu_runner.run_qemu_emulation(fake_img, []) is False


def test_main_rejects_missing_sketch(tmp_path: Path) -> None:
    missing_sketch = tmp_path / "missing.ino"
    with pytest.raises(typer.BadParameter, match="Sketch does not exist"):
        qemu_runner.main(sketch=missing_sketch)


def test_main_rejects_missing_firmware(tmp_path: Path) -> None:
    missing_fw = tmp_path / "missing.bin"
    with pytest.raises(SystemExit) as exc:
        qemu_runner.main(firmware=missing_fw)
    assert exc.value.code == 1


def test_run_qemu_emulation_full_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_img = tmp_path / "firmware_flash.bin"
    fake_img.write_text("IMG", encoding="utf-8")
    log_dir = tmp_path / "logs"

    def mock_ensure_gateway(state: Any) -> subprocess.Popen[str] | None:
        _ = state
        return None

    def mock_tcp_ready(host: str, port: int, timeout: float = 30.0, interval: float = 0.5) -> bool:
        _ = (host, port, timeout, interval)
        return True

    def mock_setup_uci(slave: str, *, device_id: str = "esp32-01") -> tuple[Path, Path, dict[str, str]]:
        _ = (slave, device_id)
        return (tmp_path / "uci", tmp_path / "db", {"FAKE": "1"})

    def mock_path_ready(path: Path, timeout: float = 5.0, interval: float = 0.05) -> bool:
        _ = (path, timeout, interval)
        return True

    def mock_run_scripts(
        scripts: list[Path],
        *,
        device_id: str = "esp32-01",
        led_builtin_pin: int = 13,
    ) -> bool:
        _ = (scripts, device_id, led_builtin_pin)
        return True

    def mock_audit() -> bool:
        return True

    def mock_start_thread(*args: Any, **kwargs: Any) -> None:
        _ = (args, kwargs)

    def mock_terminate(procs: Any, timeout: float = 5.0) -> None:
        _ = (procs, timeout)

    monkeypatch.setattr(qemu_runner, "find_qemu_binary", _mock_find_qemu)
    monkeypatch.setattr(qemu_runner.shutil, "which", _mock_which_usr)
    monkeypatch.setattr(qemu_runner, "ensure_cloud_gateway", mock_ensure_gateway)
    monkeypatch.setattr(qemu_runner, "wait_for_tcp_ready", mock_tcp_ready)
    monkeypatch.setattr(qemu_runner, "setup_fake_uci", mock_setup_uci)
    monkeypatch.setattr(qemu_runner, "wait_for_path_ready", mock_path_ready)
    monkeypatch.setattr(qemu_runner, "run_client_scripts", mock_run_scripts)
    monkeypatch.setattr(qemu_runner, "audit_status", mock_audit)

    class MockPopen:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.stdout = None
            self.stderr = None
            self.returncode = 0

        def poll(self) -> int | None:
            return None

    monkeypatch.setattr(subprocess, "Popen", MockPopen)
    monkeypatch.setattr(qemu_runner, "start_daemon_thread", mock_start_thread)
    monkeypatch.setattr(qemu_runner, "terminate_process_tree", mock_terminate)

    # Mock QemuState to immediately set events
    original_state_cls = qemu_runner.QemuState

    def mock_state() -> qemu_runner.QemuState:
        s = original_state_cls()
        s.listening_event.set()
        s.sync_event.set()
        s.capabilities_event.set()
        return s

    monkeypatch.setattr(qemu_runner, "QemuState", mock_state)

    passed = qemu_runner.run_qemu_emulation(
        flash_image=fake_img,
        test_scripts=[],
        timeout_seconds=5.0,
        log_dir=log_dir,
    )
    assert passed is True
    assert (log_dir / "qemu_execution.log").exists()
