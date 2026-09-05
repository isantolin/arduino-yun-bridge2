"""Tests for psutil-based process lifecycle management, cleanup, and telemetry (SIL-2)."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.state.context import ProcessContext, create_runtime_state
from tools.emulation.process_utils import terminate_pid_tree, terminate_process_tree


def test_build_status_snapshot_populates_psutil_telemetry(runtime_config: RuntimeConfig) -> None:
    """Validate that build_status_snapshot() populates SystemStatus and ProcessStats natively via psutil."""
    state = create_runtime_state(runtime_config)
    try:
        status = state.build_status_snapshot()
        assert isinstance(status, pb.BridgeStatus)
        assert status.HasField("system")

        # Validate SystemStatus telemetry fields
        sys = status.system
        assert sys.memory_total_bytes > 0
        assert sys.memory_available_bytes > 0
        assert sys.uptime_seconds >= 0.0
        assert sys.load_avg_1m >= 0.0
        assert 0.0 <= sys.cpu_percent <= 100.0

        # Validate ProcessStats
        assert len(status.process_stats) >= 1
        daemon_proc = status.process_stats[0]
        assert len(daemon_proc.name) > 0
        assert daemon_proc.memory_rss_bytes > 0
        assert daemon_proc.cpu_percent >= 0.0
    finally:
        state.cleanup()


def test_build_status_snapshot_with_running_subprocess(runtime_config: RuntimeConfig) -> None:
    """Validate that active running_processes are reflected in status.process_stats."""
    state = create_runtime_state(runtime_config)
    try:
        mock_handle = MagicMock()
        mock_handle.pid = 998877

        mock_psutil_proc = MagicMock()
        mock_psutil_proc.name.return_value = "mock-worker"
        mock_psutil_proc.cpu_percent.return_value = 12.5
        mock_psutil_proc.memory_info.return_value.rss = 1048576

        state.running_processes[998877] = ProcessContext(mock_handle)

        with patch("psutil.pid_exists", return_value=True), patch("psutil.Process", return_value=mock_psutil_proc):
            status = state.build_status_snapshot()
            assert any(p.name == "subproc-998877" for p in status.process_stats)
            subproc = next(p for p in status.process_stats if p.name == "subproc-998877")
            assert subproc.cpu_percent == 12.5
            assert subproc.memory_rss_bytes == 1048576
    finally:
        state.cleanup()


def test_build_status_snapshot_exception_fallback(runtime_config: RuntimeConfig) -> None:
    """Validate safe state defaults when psutil metrics query encounters OSError."""
    state = create_runtime_state(runtime_config)
    try:
        with patch("psutil.virtual_memory", side_effect=OSError("Access error")):
            status = state.build_status_snapshot()
            assert status.HasField("system")
            assert status.system.memory_total_bytes == 0
            assert status.system.memory_available_bytes == 0
    finally:
        state.cleanup()


def test_context_cleanup_recursive_child_termination(runtime_config: RuntimeConfig) -> None:
    """Validate that state.cleanup() recursively terminates all child processes."""
    state = create_runtime_state(runtime_config)

    mock_handle = MagicMock()
    mock_handle.pid = 12345
    mock_handle.terminate = MagicMock()

    mock_parent = MagicMock()
    mock_child1 = MagicMock()
    mock_child2 = MagicMock()
    mock_parent.children.return_value = [mock_child1, mock_child2]

    state.running_processes[12345] = ProcessContext(mock_handle)

    with patch("psutil.pid_exists", return_value=True), patch("psutil.Process", return_value=mock_parent):
        state.cleanup()
        assert len(state.running_processes) == 0
        assert mock_child1.terminate.called
        assert mock_child2.terminate.called
        assert mock_parent.terminate.called
        assert mock_handle.terminate.called


def test_terminate_process_tree_graceful_and_escalation() -> None:
    """Validate terminate_process_tree terminating hierarchy and escalating surviving procs."""
    mock_popen1 = MagicMock(spec=subprocess.Popen)
    mock_popen1.pid = 11111
    mock_popen2 = MagicMock(spec=subprocess.Popen)
    mock_popen2.pid = 22222

    mock_proc1 = MagicMock()
    mock_proc2 = MagicMock()
    mock_child = MagicMock()
    mock_proc1.children.return_value = [mock_child]
    mock_proc2.children.return_value = []

    def fake_process(pid: int) -> MagicMock:
        if pid == 11111:
            return mock_proc1
        return mock_proc2

    with (
        patch("psutil.pid_exists", return_value=True),
        patch("psutil.Process", side_effect=fake_process),
        patch("psutil.wait_procs", return_value=([mock_proc1], [mock_proc2])),
    ):
        terminate_process_tree([mock_popen1, mock_popen2], timeout=1.0)
        assert mock_child.terminate.called
        assert mock_proc1.terminate.called
        assert mock_proc2.terminate.called
        # Proc2 survived timeout -> escalated to kill()
        assert mock_proc2.kill.called


def test_terminate_pid_tree() -> None:
    """Validate terminate_pid_tree by root PID."""
    mock_proc = MagicMock()
    mock_child = MagicMock()
    mock_proc.children.return_value = [mock_child]

    with (
        patch("psutil.pid_exists", return_value=True),
        patch("psutil.Process", return_value=mock_proc),
        patch("psutil.wait_procs", return_value=([mock_proc], [])),
    ):
        terminate_pid_tree(55555, timeout=1.0)
        assert mock_child.terminate.called
        assert mock_proc.terminate.called
