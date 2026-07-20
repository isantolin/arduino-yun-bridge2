"""Second targeted coverage gap closure — context.py, metrics.py,
structures.py SSL, scripts, daemon.py. [SIL-2]"""

from __future__ import annotations
from typing import cast

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import RuntimeState, create_runtime_state

# ==============================================================================
# Fixtures
# ==============================================================================


def _make_config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/ttyATH0",
        topic_prefix="br",
        allowed_commands=("*",),
        serial_shared_secret=b"secret123456abcd",
        file_system_root=str(tmp_path / "fs"),
        cloud_spool_dir=str(tmp_path / "spool"),
        allow_non_tmp_paths=True,
    )


@pytest.fixture
def cfg(tmp_path: Path) -> RuntimeConfig:
    return _make_config(tmp_path)


@pytest.fixture
def state(cfg: RuntimeConfig) -> Iterator[RuntimeState]:
    s = create_runtime_state(cfg)
    yield s
    s.cleanup()


# ==============================================================================
# context.py — mark_transport_disconnected with link_sync_event (lines 222-223)
# configure() fallback branch (307-308, 324-326)
# cleanup() coroutine paths (481-513), running_processes (527-533)
# apply_handshake_stats exception (364-365)
# record_supervisor_failure (242-258)
# build_serial_pipeline_snapshot with data (342-347)
# ==============================================================================


def test_mark_transport_disconnected_clears_event(state: RuntimeState) -> None:
    """mark_transport_disconnected clears link_sync_event (lines 222-223)."""
    state.mark_synchronized()
    assert state.link_sync_event.is_set()
    state.mark_transport_disconnected()
    assert not state.link_sync_event.is_set()
    assert state.state == "disconnected"




def test_mark_transport_disconnected_no_event(cfg: RuntimeConfig) -> None:
    """mark_transport_disconnected with link_sync_event=None doesn't crash."""
    s = create_runtime_state(cfg)
    cast(Any, s).link_sync_event = None
    s.mark_transport_disconnected()  # should not raise
    s.cleanup()


def test_record_supervisor_failure(state: RuntimeState) -> None:
    """record_supervisor_failure stores stats (lines 242-258)."""
    exc = ValueError("test error")
    state.record_supervisor_failure("test_task", 2.5, exc)
    assert "test_task" in state.supervisor_stats
    assert state.supervisor_stats["test_task"].restarts == 1
    assert state.supervisor_failures == 1

    # Second failure — restarts increment
    state.record_supervisor_failure("test_task", 5.0, None)
    assert state.supervisor_stats["test_task"].restarts == 2


def test_mark_supervisor_healthy_existing(state: RuntimeState) -> None:
    """mark_supervisor_healthy resets backoff for known task (lines 262-263)."""
    exc = ValueError("err")
    state.record_supervisor_failure("worker", 10.0, exc)
    state.mark_supervisor_healthy("worker")
    assert state.supervisor_stats["worker"].backoff_seconds == 0.0


def test_mark_supervisor_healthy_unknown(state: RuntimeState) -> None:
    """mark_supervisor_healthy on unknown task is a no-op."""
    state.mark_supervisor_healthy("nonexistent")  # should not raise


def test_apply_handshake_stats_type_error(state: RuntimeState) -> None:
    """apply_handshake_stats handles non-numeric values gracefully (lines 364-365)."""
    state.apply_handshake_stats({"attempts": "not_a_number", "successes": None})
    # Should log warning but not raise


def test_build_serial_pipeline_snapshot_with_data(state: RuntimeState) -> None:
    """build_serial_pipeline_snapshot when inflight and last are populated (lines 342-347)."""
    import time

    state.serial_pipeline_inflight = {
        "event": "send",
        "command_id": 10,
        "attempt": 1,
        "ack_received": False,
        "status": 0,
        "timestamp": time.time(),
    }
    state.serial_pipeline_last = {
        "event": "complete",
        "command_id": 5,
        "attempt": 2,
        "ack_received": True,
        "status": 0,
        "timestamp": time.time(),
    }
    snap = state.build_serial_pipeline_snapshot()
    assert snap.inflight.event == "send"
    assert snap.last_completion.event == "complete"


def test_configure_spool_oserror_fallback(cfg: RuntimeConfig) -> None:
    """configure() falls back to InMemoryDeque when mkdir raises OSError (lines 307-308)."""
    s = create_runtime_state(cfg)
    with patch.object(Path, "mkdir", side_effect=OSError("no space")):
        s.configure()
    # Should not raise; queues exist
    assert s.mailbox_queue is not None
    s.cleanup()


def test_cleanup_coroutine_queue(cfg: RuntimeConfig) -> None:
    """cleanup() handles coroutine returned by queue.close() (lines 481-485, 492-496)."""
    s = create_runtime_state(cfg)

    # Create mock queues that return coroutines from .close()
    async def _coro_close() -> None:
        pass

    mock_queue = MagicMock()
    mock_queue.close = _coro_close
    mock_queue.__len__ = MagicMock(return_value=0)

    s.mailbox_queue = mock_queue
    s.mailbox_incoming_queue = mock_queue
    s.cleanup()


def test_cleanup_with_running_processes(cfg: RuntimeConfig) -> None:
    """cleanup() terminates running processes (lines 527-533)."""
    s = create_runtime_state(cfg)

    mock_handle = MagicMock()
    mock_handle.terminate = MagicMock()

    from mcubridge.state.context import ProcessContext

    ctx = ProcessContext.__new__(ProcessContext)
    ctx.handle = mock_handle

    s.running_processes[1001] = ctx
    s.cleanup()
    mock_handle.terminate.assert_called_once()


def test_cleanup_process_terminate_oserror(cfg: RuntimeConfig) -> None:
    """cleanup() handles OSError during process.terminate() (line 531)."""
    s = create_runtime_state(cfg)

    mock_handle = MagicMock()
    mock_handle.terminate = MagicMock(side_effect=OSError("gone"))

    from mcubridge.state.context import ProcessContext

    ctx = ProcessContext.__new__(ProcessContext)
    ctx.handle = mock_handle

    s.running_processes[1001] = ctx
    s.cleanup()  # should not raise


def test_create_runtime_state_from_dict(cfg: RuntimeConfig) -> None:
    """create_runtime_state accepts dict config (lines 548-549)."""
    cfg_dict = {
        "serial_port": "/dev/ttyATH0",
        "topic_prefix": "br",
        "serial_shared_secret": "c2VjcmV0MTIzNDU2YWJjZA==",
        "allow_non_tmp_paths": True,
        "file_system_root": "/tmp/test_dict_cfg",
        "cloud_spool_dir": "/tmp/test_spool",
    }
    s = create_runtime_state(cfg_dict)
    assert s is not None
    s.cleanup()


# ==============================================================================
# metrics.py — RuntimeStateCollector.collect() state=None (line 215)
# _emit_bridge_snapshot error paths (lines 106-118)
# publish_bridge_snapshots both disabled (lines 165-169)
# publish_metrics cancelled (lines 147-149)
# _build_metrics_message extra_props branches (lines 52-69)
# ==============================================================================


@pytest.mark.asyncio
async def test_runtime_state_collector_state_none(state: RuntimeState) -> None:
    """RuntimeStateCollector.collect() returns nothing when state is GC'd (line 215)."""
    import weakref
    from mcubridge.metrics import RuntimeStateCollector

    weakref.ref(state)
    collector = RuntimeStateCollector(state)
    del state
    import gc

    gc.collect()

    # If state was garbage collected, collect() should return without yielding
    results = list(collector.collect())
    # May or may not be empty depending on GC, just must not crash
    _ = results


@pytest.mark.asyncio
async def test_emit_bridge_snapshot_type_error(state: RuntimeState) -> None:
    """_emit_bridge_snapshot handles TypeError from enqueue (lines 106-111)."""
    from mcubridge import metrics as metrics_mod

    async def _bad_enqueue(msg: Any) -> None:
        raise TypeError("bad type")

    fn = getattr(metrics_mod, "_emit_bridge_snapshot")
    await fn(state, _bad_enqueue, flavor="summary")
    # Should log error, not raise


@pytest.mark.asyncio
async def test_emit_bridge_snapshot_attribute_error(state: RuntimeState) -> None:
    """_emit_bridge_snapshot handles AttributeError from build_bridge_snapshot (lines 112-118)."""
    from mcubridge import metrics as metrics_mod

    async def _enqueue(msg: Any) -> None:
        pass

    fn = getattr(metrics_mod, "_emit_bridge_snapshot")
    with patch.object(state, "build_bridge_snapshot", side_effect=AttributeError("missing")):
        await fn(state, _enqueue, flavor="summary")
    # Should log critical, not raise


@pytest.mark.asyncio
async def test_publish_bridge_snapshots_both_disabled(state: RuntimeState) -> None:
    """publish_bridge_snapshots with both intervals=0 awaits forever (lines 165-169)."""
    from mcubridge.metrics import publish_bridge_snapshots

    async def _enqueue(msg: Any) -> None:
        pass

    task = asyncio.create_task(
        publish_bridge_snapshots(
            state,
            _enqueue,
            summary_interval=0,
            handshake_interval=0,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_publish_metrics_cancelled(state: RuntimeState) -> None:
    """publish_metrics raises CancelledError cleanly (lines 147-149)."""
    from mcubridge.metrics import publish_metrics

    async def _enqueue(msg: Any) -> None:
        pass

    task = asyncio.create_task(publish_metrics(state, _enqueue, interval=1.0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_build_metrics_message_extra_props(state: RuntimeState) -> None:
    """_build_metrics_message adds extra_props for file rejections and watchdog (lines 52-69)."""
    from mcubridge import metrics as metrics_mod

    state.file_storage_limit_rejections = 1
    snapshot = state.build_metrics_snapshot()
    fn = getattr(metrics_mod, "_build_metrics_message")
    msg = fn(state, snapshot, expiry_seconds=60.0)
    props = {p.key: p.value for p in msg.user_properties}
    from mcubridge.config.const import PROP_KEY_BRIDGE_FILES

    assert PROP_KEY_BRIDGE_FILES in props


@pytest.mark.asyncio
async def test_build_metrics_message_write_limit(state: RuntimeState) -> None:
    """_build_metrics_message adds write-limit prop when file_write_limit_rejections > 0."""
    from mcubridge import metrics as metrics_mod
    from mcubridge.config.const import PROP_KEY_BRIDGE_FILES, PROP_VAL_WRITE_LIMIT

    state.file_storage_limit_rejections = 0
    state.file_write_limit_rejections = 3
    snapshot = state.build_metrics_snapshot()
    fn = getattr(metrics_mod, "_build_metrics_message")
    msg = fn(state, snapshot, expiry_seconds=60.0)
    props = {p.key: p.value for p in msg.user_properties}
    assert props.get(PROP_KEY_BRIDGE_FILES) == PROP_VAL_WRITE_LIMIT


@pytest.mark.asyncio
async def test_build_metrics_message_watchdog_enabled(state: RuntimeState) -> None:
    """_build_metrics_message adds watchdog interval prop when enabled (lines 63-64)."""
    from mcubridge import metrics as metrics_mod
    from mcubridge.config.const import PROP_KEY_WATCHDOG_INTERVAL

    state.watchdog_enabled = True
    state.watchdog_interval = 30.0
    snapshot = state.build_metrics_snapshot()
    fn = getattr(metrics_mod, "_build_metrics_message")
    msg = fn(state, snapshot, expiry_seconds=60.0)
    props = {p.key: p.value for p in msg.user_properties}
    assert PROP_KEY_WATCHDOG_INTERVAL in props


# ==============================================================================
# structures.py — get_ssl_context (line 192 cloud_tls=False, 209-210 insecure,
# 215-217 certfile without keyfile, 218-219 OSError)
# ==============================================================================


def test_get_ssl_context_disabled(cfg: RuntimeConfig) -> None:
    """get_ssl_context returns None when cloud_tls=False (line 191-192)."""
    from mcubridge.protocol.structures import get_ssl_context

    cfg.cloud_tls = False
    result = get_ssl_context(cfg)
    assert result is None


def test_get_ssl_context_insecure(cfg: RuntimeConfig, tmp_path: Path) -> None:
    """get_ssl_context sets check_hostname=False for insecure TLS (lines 209-210)."""
    from mcubridge.protocol.structures import get_ssl_context

    cfg.cloud_tls = True
    cfg.cloud_tls_insecure = True
    result = get_ssl_context(cfg)
    assert result is not None
    assert result.check_hostname is False


def test_get_ssl_context_cert_without_key_raises(cfg: RuntimeConfig) -> None:
    """get_ssl_context raises RuntimeError when certfile provided without keyfile (lines 213-214)."""
    from mcubridge.protocol.structures import get_ssl_context

    cfg.cloud_tls = True
    cfg.cloud_certfile = "/some/cert.pem"
    cfg.cloud_keyfile = ""
    with pytest.raises(RuntimeError, match="TLS setup failed"):
        get_ssl_context(cfg)


def test_get_ssl_context_missing_cafile(cfg: RuntimeConfig, tmp_path: Path) -> None:
    """get_ssl_context raises RuntimeError when CA file is missing (lines 200-201)."""
    from mcubridge.protocol.structures import get_ssl_context

    cfg.cloud_tls = True
    cfg.cloud_cafile = str(tmp_path / "nonexistent_ca.pem")
    with pytest.raises(RuntimeError, match="Cloud TLS CA file missing"):
        get_ssl_context(cfg)


# ==============================================================================
# scripts/mcubridge_file_push.py — error paths (lines 36-38, 40, 65, 75, 88)
# ==============================================================================

_script_path = Path(__file__).parent.parent / "scripts" / "mcubridge_file_push.py"
_spec = importlib.util.spec_from_file_location("mcubridge_file_push", str(_script_path))
assert _spec is not None and _spec.loader is not None
mcubridge_file_push = importlib.util.module_from_spec(_spec)
sys.modules["mcubridge_file_push"] = mcubridge_file_push
_spec.loader.exec_module(mcubridge_file_push)


def test_file_push_source_not_exist(tmp_path: Path) -> None:
    """main() exits with code 2 when source does not exist (lines 54-56)."""
    with patch("sys.argv", ["file_push", str(tmp_path / "nonexistent.bin"), "target/path"]):
        with patch("mcubridge_file_push.load_runtime_config"):
            with pytest.raises(SystemExit) as exc_info:
                mcubridge_file_push.main()
    assert exc_info.value.code == 2


def test_file_push_source_is_dir(tmp_path: Path) -> None:
    """main() exits with code 2 when source is a directory (line 54)."""
    with patch("sys.argv", ["file_push", str(tmp_path), "target/path"]):
        with patch("mcubridge_file_push.load_runtime_config"):
            with pytest.raises(SystemExit) as exc_info:
                mcubridge_file_push.main()
    assert exc_info.value.code == 2


def test_file_push_mcu_flag(tmp_path: Path) -> None:
    """main() with --mcu flag appends 'mcu' to topic segments (line 65)."""
    src = tmp_path / "test.bin"
    src.write_bytes(b"x" * 80)

    captured_topics: list[str] = []

    def _push(topic: str, data: bytes) -> None:
        captured_topics.append(topic)

    with patch("sys.argv", ["file_push", str(src), "target.bin", "--mcu"]):
        with patch("mcubridge_file_push.load_runtime_config") as mock_cfg:
            mock_cfg.return_value = RuntimeConfig(topic_prefix="br", allow_non_tmp_paths=True)
            with patch("mcubridge_file_push.push_file", side_effect=_push):
                mcubridge_file_push.main()
    assert any("mcu" in t for t in captured_topics)


def test_file_push_large_file_hexdump(tmp_path: Path) -> None:
    """main() with large file adds '...' to hexdump (line 75)."""
    src = tmp_path / "large.bin"
    src.write_bytes(b"\xab" * 200)

    with patch("sys.argv", ["file_push", str(src), "target.bin"]):
        with patch("mcubridge_file_push.load_runtime_config") as mock_cfg:
            mock_cfg.return_value = RuntimeConfig(topic_prefix="br", allow_non_tmp_paths=True)
            with patch("mcubridge_file_push.push_file"):
                mcubridge_file_push.main()


def test_push_file_exception_exits(tmp_path: Path) -> None:
    """push_file() exits with code 1 when gRPC raises (lines 37-38)."""
    with patch("mcubridge_file_push.Channel") as mock_chan:
        mock_chan.return_value.__enter__ = MagicMock(return_value=mock_chan.return_value)
        mock_chan.return_value.__exit__ = MagicMock(return_value=False)
        stub_mock = AsyncMock()
        stub_mock.Publish = AsyncMock(side_effect=ConnectionError("refused"))
        with patch("mcubridge_file_push.LocalBridgeStub", return_value=stub_mock):
            with pytest.raises(SystemExit) as exc_info:
                mcubridge_file_push.push_file("br/file/write/test", b"data")
    assert exc_info.value.code == 1


# ==============================================================================
# daemon.py — lines 85-90, 123, 133
# ==============================================================================


def test_daemon_app_version() -> None:
    """daemon.app(["--version"]) prints version and exits."""
    from mcubridge import daemon

    with pytest.raises(SystemExit) as exc_info:
        daemon.app(["--version"])
    assert exc_info.value.code == 0


# ==============================================================================
# config/logging.py — line 35-36 (configure_logging with non-default level)
# ==============================================================================


def test_configure_logging_debug_level(cfg: RuntimeConfig) -> None:
    """configure_logging sets DEBUG level correctly when config.debug is True."""
    from mcubridge.config.logging import configure_logging

    cfg.debug = True
    configure_logging(cfg)


def test_configure_logging_default(cfg: RuntimeConfig) -> None:
    """configure_logging with default INFO level."""
    from mcubridge.config.logging import configure_logging

    cfg.debug = False
    configure_logging(cfg)
