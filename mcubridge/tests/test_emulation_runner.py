"""Unit tests for tools/emulation/emulation_runner.py (SIL-2 / Rule 11 / Rule 18 / Rule 19)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tools.emulation import emulation_runner


def test_default_output_lines() -> None:
    lines = emulation_runner.default_output_lines()
    assert isinstance(lines, list)
    assert len(lines) == 0


def test_emulation_state_on_line() -> None:
    state = emulation_runner.EmulationState()
    assert not state.sync_event.is_set()
    assert not state.capabilities_event.is_set()
    assert not state.listening_event.is_set()

    state.on_line("  hello world  ", "test_source")
    state.on_line("   ", "empty_source")  # Empty line ignored

    assert len(state.output_lines) == 1
    assert state.output_lines[0] == ("test_source", "hello world")

    state.on_line("QEMU waiting for connection on: 127.0.0.1:12346", "qemu")
    assert state.listening_event.is_set()

    state.on_line("Handshake synchronization complete", "daemon-stdout")
    assert state.sync_event.is_set()

    state.on_line("MCU capabilities received: ...", "daemon-stdout")
    assert state.capabilities_event.is_set()


def test_ensure_cloud_gateway_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    def mock_wait_tcp(host: str, port: int, timeout: float = 1.0, interval: float = 0.5) -> bool:
        _ = (host, port, timeout, interval)
        return True

    monkeypatch.setattr(emulation_runner, "wait_for_tcp_ready", mock_wait_tcp)
    state = emulation_runner.EmulationState()
    proc = emulation_runner.ensure_cloud_gateway(state)
    assert proc is None


def test_ensure_cloud_gateway_spawn_success(monkeypatch: pytest.MonkeyPatch) -> None:
    state = emulation_runner.EmulationState()
    ready_sequence = [False, True]

    def mock_tcp_ready(host: str, port: int, timeout: float = 30.0, interval: float = 0.5) -> bool:
        _ = (host, port, timeout, interval)
        return ready_sequence.pop(0)

    monkeypatch.setattr(emulation_runner, "wait_for_tcp_ready", mock_tcp_ready)

    class MockPopen:
        stdout = None

    def mock_popen(*args: Any, **kwargs: Any) -> MockPopen:
        _ = (args, kwargs)
        return MockPopen()

    def mock_start_thread(*args: Any, **kwargs: Any) -> None:
        _ = (args, kwargs)

    monkeypatch.setattr(subprocess, "Popen", mock_popen)
    monkeypatch.setattr(emulation_runner, "start_daemon_thread", mock_start_thread)

    proc = emulation_runner.ensure_cloud_gateway(state)
    assert proc is not None


def test_ensure_cloud_gateway_spawn_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    state = emulation_runner.EmulationState()

    def mock_tcp_ready(host: str, port: int, timeout: float = 30.0, interval: float = 0.5) -> bool:
        _ = (host, port, timeout, interval)
        return False

    monkeypatch.setattr(emulation_runner, "wait_for_tcp_ready", mock_tcp_ready)

    class MockPopen:
        stdout = None

    def mock_popen(*args: Any, **kwargs: Any) -> MockPopen:
        _ = (args, kwargs)
        return MockPopen()

    terminated: list[bool] = []

    def mock_terminate(procs: Any, timeout: float = 1.0) -> None:
        _ = (procs, timeout)
        terminated.append(True)

    def mock_start_thread(*args: Any, **kwargs: Any) -> None:
        _ = (args, kwargs)

    monkeypatch.setattr(subprocess, "Popen", mock_popen)
    monkeypatch.setattr(emulation_runner, "start_daemon_thread", mock_start_thread)
    monkeypatch.setattr(emulation_runner, "terminate_process_tree", mock_terminate)

    proc = emulation_runner.ensure_cloud_gateway(state)
    assert proc is None
    assert len(terminated) == 1


def test_prepare_emulator_fs(tmp_path: Path) -> None:
    fs_root = tmp_path / "emulator_fs"
    fs_root.mkdir()
    (fs_root / "dummy.txt").write_text("dummy", encoding="utf-8")

    emulation_runner.prepare_emulator_fs(fs_root)
    assert fs_root.exists()
    assert not (fs_root / "dummy.txt").exists()


def test_wait_for_daemon_sync_success(tmp_path: Path) -> None:
    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps({"bridge": {"is_synchronized": True}}),
        encoding="utf-8",
    )
    synced = emulation_runner.wait_for_daemon_sync(status_file, timeout=1.0)
    assert synced is True

    status_file.write_text(
        json.dumps({"bridge": {"serial_link": {"synchronised": True}}}),
        encoding="utf-8",
    )
    assert emulation_runner.wait_for_daemon_sync(status_file, timeout=1.0) is True

    status_file.write_text(
        json.dumps({"bridge": {"handshake": {"synchronised": True}}}),
        encoding="utf-8",
    )
    assert emulation_runner.wait_for_daemon_sync(status_file, timeout=1.0) is True


def test_wait_for_daemon_sync_failure(tmp_path: Path) -> None:
    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps({"bridge": {"is_synchronized": False}}),
        encoding="utf-8",
    )
    synced = emulation_runner.wait_for_daemon_sync(status_file, timeout=0.1)
    assert synced is False


def test_run_client_script_with_main(tmp_path: Path) -> None:
    script = tmp_path / "test_script.py"
    result_file = tmp_path / "led_pin.txt"
    script.write_text(
        "from pathlib import Path\n"
        "def main(host=None, port=None, device_id=None, led_builtin_pin=None):\n"
        f"    Path({str(result_file)!r}).write_text(str(device_id) + ':' + str(led_builtin_pin), encoding='utf-8')\n",
        encoding="utf-8",
    )
    passed = emulation_runner.run_client_script(
        script,
        device_id="custom-01",
        led_builtin_pin=7,
    )
    assert passed is True
    assert result_file.read_text(encoding="utf-8") == "custom-01:7"


def test_run_client_script_exit_codes_and_runpy_fallback(tmp_path: Path) -> None:
    script_pass = tmp_path / "pass_exit.py"
    script_pass.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    assert emulation_runner.run_client_script(script_pass) is True

    script_fail = tmp_path / "fail_exit.py"
    script_fail.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    assert emulation_runner.run_client_script(script_fail) is False

    script_runpy = tmp_path / "test_runpy.py"
    output_file = tmp_path / "out.txt"
    script_runpy.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({str(output_file)!r}).write_text(' '.join(sys.argv), encoding='utf-8')\n",
        encoding="utf-8",
    )
    assert emulation_runner.run_client_script(script_runpy, device_id="mkr-special") is True
    assert "--device-id mkr-special" in output_file.read_text(encoding="utf-8")


def test_run_client_script_failure_and_batch(tmp_path: Path) -> None:
    script = tmp_path / "fail_script.py"
    script.write_text(
        "def main(host=None, port=None, device_id=None):\n    raise RuntimeError('simulated test failure')\n",
        encoding="utf-8",
    )
    assert emulation_runner.run_client_script(script, device_id="custom-01") is False

    s1 = tmp_path / "s1.py"
    s1.write_text("def main(host=None, port=None, device_id=None):\n    pass\n", encoding="utf-8")
    missing = tmp_path / "does_not_exist.py"
    assert emulation_runner.run_client_scripts([s1, missing], led_builtin_pin=9) is True


def test_audit_post_execution_status_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    status_file = tmp_path / "status.json"
    assert emulation_runner.audit_post_execution_status(status_file) is True

    status_file.write_text(json.dumps({"metrics": {}, "bridge": {}}), encoding="utf-8")

    def mock_audit_clean(data: dict[str, Any]) -> list[str]:
        _ = data
        return []

    monkeypatch.setattr(emulation_runner, "audit_status_dict", mock_audit_clean)
    assert emulation_runner.audit_post_execution_status(status_file) is True


def test_audit_post_execution_status_anomalies_and_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    status_file = tmp_path / "status.json"
    status_file.write_text("invalid json", encoding="utf-8")
    assert emulation_runner.audit_post_execution_status(status_file) is False

    status_file.write_text(json.dumps({"metrics": {}, "bridge": {}}), encoding="utf-8")

    def mock_audit_anomalies(data: dict[str, Any]) -> list[str]:
        _ = data
        return ["Handshake failure streak > 0"]

    monkeypatch.setattr(emulation_runner, "audit_status_dict", mock_audit_anomalies)
    assert emulation_runner.audit_post_execution_status(status_file) is False


def test_run_emulation_pty_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def mock_ensure_gateway(state: emulation_runner.EmulationState) -> subprocess.Popen[str] | None:
        _ = state
        return None

    def mock_wait_path(path: str | Path, timeout: float = 10.0, interval: float = 0.1) -> bool:
        _ = (path, timeout, interval)
        return False

    def mock_terminate_tree(procs: list[subprocess.Popen[Any]], timeout: float = 1.0) -> None:
        _ = (procs, timeout)

    mock_popen: subprocess.Popen[Any] = MagicMock()
    mock_popen.stderr = None

    def mock_popen_fn(*args: object, **kwargs: object) -> subprocess.Popen[Any]:
        _ = (args, kwargs)
        return mock_popen

    monkeypatch.setattr(emulation_runner, "ensure_cloud_gateway", mock_ensure_gateway)
    monkeypatch.setattr(emulation_runner, "wait_for_path_ready", mock_wait_path)
    monkeypatch.setattr(emulation_runner.subprocess, "Popen", mock_popen_fn)
    monkeypatch.setattr(emulation_runner, "terminate_process_tree", mock_terminate_tree)

    firmware = tmp_path / "fake_firmware"
    firmware.touch()

    with pytest.raises(SystemExit) as exc_info:
        emulation_runner.run_emulation(firmware_path=firmware)

    assert exc_info.value.code == 1
