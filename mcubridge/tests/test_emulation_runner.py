"""Unit tests for tools/emulation/emulation_runner.py (SIL-2 / Rule 11 / Rule 18)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tools.emulation import emulation_runner


def test_default_output_lines() -> None:
    lines = emulation_runner._default_output_lines()
    assert isinstance(lines, list)
    assert len(lines) == 0


def test_emulation_state_on_line() -> None:
    state = emulation_runner.EmulationState()
    state.on_line("  hello world  ", "test_source")
    state.on_line("   ", "empty_source")  # Empty line ignored

    assert len(state.output_lines) == 1
    assert state.output_lines[0] == ("test_source", "hello world")


def test_ensure_cloud_gateway_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emulation_runner, "wait_for_tcp_ready", lambda *args, **kwargs: True)
    state = emulation_runner.EmulationState()
    proc = emulation_runner._ensure_cloud_gateway(state)
    assert proc is None


def test_prepare_emulator_fs(tmp_path: Path) -> None:
    fs_root = tmp_path / "emulator_fs"
    fs_root.mkdir()
    (fs_root / "dummy.txt").write_text("dummy", encoding="utf-8")

    emulation_runner._prepare_emulator_fs(fs_root)
    assert fs_root.exists()
    assert not (fs_root / "dummy.txt").exists()


def test_wait_for_daemon_sync_success(tmp_path: Path) -> None:
    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps({"bridge": {"is_synchronized": True}}),
        encoding="utf-8",
    )
    synced = emulation_runner._wait_for_daemon_sync(status_file, timeout=1.0)
    assert synced is True


def test_wait_for_daemon_sync_failure(tmp_path: Path) -> None:
    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps({"bridge": {"is_synchronized": False}}),
        encoding="utf-8",
    )
    synced = emulation_runner._wait_for_daemon_sync(status_file, timeout=0.1)
    assert synced is False


def test_run_client_script_with_main(tmp_path: Path) -> None:
    script = tmp_path / "test_script.py"
    script.write_text(
        "executed_args = {}\n"
        "def main(host=None, port=None, device_id=None):\n"
        "    global executed_args\n"
        "    executed_args['device_id'] = device_id\n",
        encoding="utf-8",
    )
    passed = emulation_runner._run_client_script(script, device_id="custom-01")
    assert passed is True


def test_run_client_script_failure(tmp_path: Path) -> None:
    script = tmp_path / "fail_script.py"
    script.write_text(
        "def main(host=None, port=None, device_id=None):\n    raise RuntimeError('simulated test failure')\n",
        encoding="utf-8",
    )
    passed = emulation_runner._run_client_script(script, device_id="custom-01")
    assert passed is False


def test_audit_post_execution_status_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps({"metrics": {}, "bridge": {}}), encoding="utf-8")

    monkeypatch.setattr(emulation_runner, "audit_status_dict", lambda data: [])
    clean = emulation_runner._audit_post_execution_status(status_file)
    assert clean is True


def test_audit_post_execution_status_anomalies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps({"metrics": {}, "bridge": {}}), encoding="utf-8")

    monkeypatch.setattr(emulation_runner, "audit_status_dict", lambda data: ["Handshake failure streak > 0"])
    clean = emulation_runner._audit_post_execution_status(status_file)
    assert clean is False


def test_run_emulation_pty_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emulation_runner, "_ensure_cloud_gateway", lambda state: None)
    monkeypatch.setattr(emulation_runner, "wait_for_path_ready", lambda *args, **kwargs: False)

    mock_popen = MagicMock()
    mock_popen.stderr = None
    monkeypatch.setattr(emulation_runner.subprocess, "Popen", lambda *args, **kwargs: mock_popen)
    monkeypatch.setattr(emulation_runner, "terminate_process_tree", lambda *args, **kwargs: None)

    firmware = tmp_path / "fake_firmware"
    firmware.touch()

    with pytest.raises(SystemExit) as exc_info:
        emulation_runner.run_emulation(firmware_path=firmware)

    assert exc_info.value.code == 1
