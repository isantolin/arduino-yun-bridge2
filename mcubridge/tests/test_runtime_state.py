"""Unit tests for mcubridge.state.context.RuntimeState (SIL-2)."""

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.state.context import ProcessContext, RuntimeState, create_runtime_state
from mcubridge.watchdog import WatchdogKeepalive
from pytest_mock import MockerFixture


def test_create_runtime_state_initializes_queues(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        assert state.cloud_publish_queue is not None
        assert state.console_to_mcu_queue is not None
        assert state.mailbox_queue is not None
    finally:
        state.cleanup()


def test_configure_updates_derived_values(runtime_config: RuntimeConfig) -> None:
    runtime_config.topic_prefix = "custom/prefix"
    state = create_runtime_state(runtime_config)
    try:
        assert state.topic_prefix == "custom/prefix"
    finally:
        state.cleanup()


def test_connection_fsm_connect_updates_state(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        assert state.is_disconnected
        state.connection_fsm.connect()
        assert state.is_connected
        assert not state.is_synchronized
        assert not state.is_disconnected
    finally:
        state.cleanup()


def test_connection_fsm_synchronize_sets_flag(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        state.connection_fsm.connect()
        state.connection_fsm.synchronize()
        assert state.is_synchronized
    finally:
        state.cleanup()


def test_record_watchdog_beat_updates_counters(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        initial_beats = state.watchdog_beats
        watchdog = WatchdogKeepalive(interval=1.0, state=state, write=lambda _: None)
        watchdog.kick()

        assert state.watchdog_beats == initial_beats + 1
        assert state.last_watchdog_beat > 0
    finally:
        state.cleanup()


def test_record_cloud_drop_increments_counter(runtime_config: RuntimeConfig) -> None:
    from unittest.mock import AsyncMock

    from mcubridge.services.runtime import BridgeService

    state = create_runtime_state(runtime_config)
    try:
        service = BridgeService(runtime_config, state, AsyncMock())
        topic = "test/topic"
        record_drop = getattr(service, "_record_cloud_drop")
        record_drop(topic)

        assert state.cloud_dropped_messages == 1
        assert state.cloud_drop_counts[topic] == 1
    finally:
        state.cleanup()


def test_build_metrics_snapshot_includes_spool_state(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        state.cloud_spool_degraded = True
        state.cloud_spool_failure_reason = "disk-full"
        state.cloud_spool_pending_messages = 3
        snapshot = state.build_metrics_snapshot()
        assert snapshot.cloud_spool_degraded
        assert snapshot.cloud_spool_failure_reason == "disk-full"
        assert snapshot.cloud_spool_pending_messages == 3
    finally:
        state.cleanup()


def test_build_bridge_snapshot(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        state.mcu_version = (2, 8, 5)
        state.mcu_capabilities = pb.Capabilities(watchdog=True, eeprom=True)

        snapshot = state.build_bridge_snapshot()
        assert snapshot.mcu_version.major == 2
        assert snapshot.mcu_version.minor == 8
        assert snapshot.mcu_version.patch == 5
        assert snapshot.capabilities.watchdog is True
    finally:
        state.cleanup()


def test_build_bridge_snapshot_capabilities_dict(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        state.mcu_capabilities = {"watchdog": True, "eeprom": False}
        snapshot = state.build_bridge_snapshot()
        assert snapshot.capabilities is not None
    finally:
        state.cleanup()


def test_build_serial_pipeline_snapshot_with_data(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        state.serial_pipeline_inflight = {
            "event": "send",
            "command_id": 0x10,
            "attempt": 1,
            "ack_received": False,
            "status": 0,
            "timestamp": 1234567890.0,
        }
        state.serial_pipeline_last = {
            "event": "complete",
            "command_id": 0x10,
            "attempt": 1,
            "ack_received": True,
            "status": 1,
            "timestamp": 1234567891.0,
        }

        snapshot = state.build_serial_pipeline_snapshot()
        assert snapshot.inflight.event == "send"
        assert snapshot.last_completion.event == "complete"
    finally:
        state.cleanup()


def test_handshake_duration_since_start(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    try:
        assert state.handshake_duration_since_start() == 0.0

        state.handshake_last_started = time.monotonic() - 1.0
        duration = state.handshake_duration_since_start()
        assert duration > 0.5
    finally:
        state.cleanup()


def test_status_writer_error_handling(runtime_config: RuntimeConfig, mocker: MockerFixture) -> None:
    state = create_runtime_state(runtime_config)
    try:
        import mcubridge.state.status as status_mod

        snapshot = state.build_status_snapshot()
        mock_file = mocker.patch("mcubridge.state.status.STATUS_FILE")
        mock_file.parent.mkdir = MagicMock(side_effect=OSError("Permission denied"))
        write_status: Callable[[object], None] = getattr(status_mod, "_write_status_file")
        write_status(snapshot)
        assert mock_file.parent.mkdir.called
    finally:
        state.cleanup()


def test_context_mark_states_without_link_sync_event(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    setattr(state, "link_sync_event", None)

    state.connection_fsm.disconnect()
    assert state.state == "disconnected"

    state.connection_fsm.synchronize()
    assert state.state == "synchronized"


def test_context_configure_safe_close_sync_resource(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)

    class SyncCloseResource:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> str:
            self.closed = True
            return "closed-synchronously"

    res = SyncCloseResource()
    state.datastore_cache = cast(Any, res)
    state.configure()
    assert res.closed is True


def test_context_cleanup_none_handle_process(runtime_config: RuntimeConfig) -> None:
    state = create_runtime_state(runtime_config)
    ctx = ProcessContext(cast(Any, None))
    state.running_processes[12345] = ctx
    state.cleanup()
    assert len(state.running_processes) == 0


def test_state_context_uncovered_branch_hardening(runtime_config: RuntimeConfig) -> None:
    st = create_runtime_state(runtime_config)
    setattr(st, "serial_tx_allowed", None)
    st.connection_fsm.connect()
    assert st.is_connected

    st2 = create_runtime_state(runtime_config)
    st2.mailbox_queue = cast(Any, object())
    st2.configure()
    assert st2.mailbox_queue is not None

    st3 = create_runtime_state(runtime_config)
    mock_proc1 = MagicMock(spec=asyncio.subprocess.Process)
    ctx1 = ProcessContext(mock_proc1)
    setattr(ctx1, "handle", None)
    st3.running_processes[1] = ctx1

    mock_proc2 = MagicMock(spec=asyncio.subprocess.Process)
    mock_proc2.pid = 99999999
    ctx2 = ProcessContext(mock_proc2)
    st3.running_processes[2] = ctx2

    snap3 = st3.build_status_snapshot()
    assert len(snap3.process_stats) >= 1


def test_context_storage_subdir_creation_error(runtime_state: RuntimeState, mocker: MockerFixture) -> None:
    mocker.patch.object(Path, "mkdir", side_effect=OSError("Permission denied"))
    res = getattr(runtime_state, "_get_storage_subdir")("test_dir")
    assert res is None


def test_context_configure_safe_close_error(runtime_state: RuntimeState) -> None:
    mock_res = MagicMock()
    mock_res.close.side_effect = OSError("close error")
    runtime_state.datastore_cache = mock_res
    runtime_state.configure()
    mock_res.close.assert_called_once()
    assert runtime_state.datastore_cache is not None


def test_context_create_spool_fallback(runtime_state: RuntimeState, mocker: MockerFixture) -> None:
    def mock_deque(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("path") != ":memory:":
            raise OSError("Disk failure")
        return MagicMock()

    mocker.patch("mcubridge.state.context.LmdbDeque", side_effect=mock_deque)
    runtime_state.configure()
    assert runtime_state.mailbox_queue is not None
    assert runtime_state.mailbox_incoming_queue is not None


def test_context_metrics_boot_time_error(runtime_state: RuntimeState, mocker: MockerFixture) -> None:
    mocker.patch("psutil.boot_time", side_effect=OSError("Cannot read uptime"))
    metrics = runtime_state.build_metrics_snapshot()
    assert metrics.uptime_seconds >= 0.0


def test_context_clean_queue_empty_and_proc_lookup_error(runtime_state: RuntimeState) -> None:
    mock_queue = MagicMock()
    mock_queue.empty.side_effect = [False, True]
    mock_queue.get_nowait.side_effect = asyncio.QueueEmpty()
    runtime_state.cloud_publish_queue = mock_queue

    mock_proc = MagicMock()
    mock_proc.pid = 99999
    mock_proc.terminate.side_effect = ProcessLookupError("No such process")
    ctx = ProcessContext(mock_proc)
    runtime_state.running_processes[99999] = ctx

    runtime_state.cleanup()
    assert len(runtime_state.running_processes) == 0
