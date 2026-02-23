import asyncio
import errno
import logging
import os
import sys
import subprocess
import time
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import msgspec
import psutil
import tenacity
from mcubridge import metrics
from mcubridge.config import logging as logging_config
from mcubridge.services.file import FileComponent, _do_write_file
from mcubridge.transport.serial import BridgeSerialProtocol, SerialTransport
from mcubridge.protocol.protocol import FileAction, Command, Status, INVALID_ID_SENTINEL
from mcubridge.services.process import ProcessComponent
from mcubridge.policy import CommandValidationError
from mcubridge.services.handshake import (
    SerialHandshakeManager,
    derive_serial_timing,
    SerialTimingWindow,
    _retry_if_false,
    _log_handshake_retry
)

def create_real_config():
    from mcubridge.config.common import get_default_config
    raw_cfg = get_default_config()
    raw_cfg.update({
        "serial_port": "/dev/ttyFake",
        "serial_shared_secret": b"valid_secret_1234",
        "mqtt_spool_dir": "/tmp/spool_final"
    })
    from mcubridge.config.settings import RuntimeConfig
    return msgspec.convert(raw_cfg, RuntimeConfig)

def create_real_state():
    from mcubridge.state.context import create_runtime_state
    return create_runtime_state(create_real_config())

# --- FileComponent Booster ---

@pytest.mark.asyncio
async def test_file_do_write_file_large_warning(tmp_path):
    fake_file = tmp_path / "large_file"
    with patch("mcubridge.services.file.FILE_LARGE_WARNING_BYTES", 10):
        with patch("mcubridge.services.file.logger.warning") as mock_warn:
            _do_write_file(fake_file, b"A" * 20)
            assert mock_warn.called

@pytest.mark.asyncio
async def test_file_handle_write_dangerous_path():
    ctx = MagicMock()
    ctx.send_frame = AsyncMock()
    comp = FileComponent(create_real_config(), create_real_state(), ctx)
    from mcubridge.protocol.structures import FileWritePacket
    payload = FileWritePacket(path="/etc/passwd", data=b"data").encode()
    assert await comp.handle_write(payload) is False
    payload = FileWritePacket(path="../traversal", data=b"data").encode()
    assert await comp.handle_write(payload) is False

@pytest.mark.asyncio
async def test_file_handle_mqtt_no_filename():
    comp = FileComponent(create_real_config(), create_real_state(), MagicMock())
    with patch("mcubridge.services.file.logger.warning") as mock_warn:
        await comp.handle_mqtt(FileAction.WRITE, [], b"")
        assert mock_warn.called

@pytest.mark.asyncio
async def test_file_perform_file_operation_unknown():
    comp = FileComponent(create_real_config(), create_real_state(), MagicMock())
    with patch.object(comp, "_get_safe_path", return_value=Path("/tmp/safe")):
        success, _, reason = await comp._perform_file_operation("UNKNOWN", "file")
        assert success is False
        assert reason == "unknown_operation"

@pytest.mark.asyncio
async def test_file_get_safe_path_base_dir_none():
    comp = FileComponent(create_real_config(), create_real_state(), MagicMock())
    with patch.object(comp, "_get_base_dir", return_value=None):
        assert comp._get_safe_path("file") is None

def test_file_normalise_filename_edge_cases():
    assert FileComponent._normalise_filename("  ") is None
    assert FileComponent._normalise_filename("..") is None
    assert FileComponent._normalise_filename("dir/../file") is None
    assert FileComponent._normalise_filename("file\x00name") is None
    assert str(FileComponent._normalise_filename("/abs/path")) == "abs/path"

@pytest.mark.asyncio
async def test_file_write_with_quota_flash_safety_fail():
    state = create_real_state()
    state.file_write_max_bytes = 1024
    state.file_storage_quota_bytes = 2048
    state.file_storage_bytes_used = 0
    comp = FileComponent(create_real_config(), state, MagicMock())
    mock_path = MagicMock(spec=Path)
    mock_path.resolve.side_effect = OSError("Resolve fail")
    with (
        patch("mcubridge.services.file.FILE_LARGE_WARNING_BYTES", 1000000),
        patch.object(comp, "_existing_file_size", return_value=0),
        patch("mcubridge.services.file._do_write_file")
    ):
        await comp._write_with_quota(mock_path, b"data")

@pytest.mark.asyncio
async def test_file_scan_directory_size_errors(tmp_path):
    res = FileComponent._scan_directory_size(tmp_path / "non_existent")
    assert res == 0
    with patch("mcubridge.services.file.scandir", side_effect=OSError("Permission denied")):
        res = FileComponent._scan_directory_size(tmp_path)
        assert res == 0

@pytest.mark.asyncio
async def test_file_get_base_dir_create_fail():
    state = create_real_state()
    state.file_system_root = "/tmp/fail_dir"
    state.allow_non_tmp_paths = True
    comp = FileComponent(create_real_config(), state, MagicMock())
    with patch("pathlib.Path.mkdir", side_effect=OSError("Read-only")):
        assert comp._get_base_dir() is None

@pytest.mark.asyncio
async def test_file_handle_mqtt_read_fail():
    comp = FileComponent(create_real_config(), create_real_state(), MagicMock())
    with patch.object(comp, "_perform_file_operation", return_value=(False, None, "read_fail")):
        await comp._handle_mqtt_read("file", None, {})

@pytest.mark.asyncio
async def test_file_handle_mqtt_remove_fail():
    comp = FileComponent(create_real_config(), create_real_state(), MagicMock())
    with patch.object(comp, "_perform_file_operation", return_value=(False, None, "remove_fail")):
        await comp._handle_mqtt_remove("file", {})

# --- Logging Booster ---

def test_logging_formatter_no_prefix():
    formatter = logging_config.StructuredLogFormatter()
    record = logging.LogRecord("other.logger", logging.INFO, "f", 1, "msg", (), None)
    res = formatter.format(record)
    assert '"logger":"other.logger"' in res

# --- Metrics Booster ---

@pytest.mark.asyncio
async def test_metrics_emit_bridge_snapshot_attr_error():
    state = MagicMock()
    state.build_bridge_snapshot.side_effect = AttributeError("Missing")
    await metrics._emit_bridge_snapshot(state, AsyncMock(), flavor="summary")

# --- Serial Booster ---

@pytest.mark.asyncio
async def test_serial_protocol_log_frame_no_payload():
    proto = BridgeSerialProtocol(MagicMock(), MagicMock(), asyncio.get_running_loop())
    from mcubridge.protocol.frame import Frame
    frame = Frame(command_id=Command.CMD_GET_VERSION.value, payload=b"")
    with patch("mcubridge.transport.serial.logger.debug") as mock_debug:
        proto._log_frame(frame, "DIR")
        assert mock_debug.call_args[0][0] == "%s %s (no payload)"
        assert mock_debug.call_args[0][1] == "DIR"
        assert mock_debug.call_args[0][2] == "CMD_GET_VERSION"

@pytest.mark.asyncio
async def test_serial_transport_blocking_reset_errors():
    transport = SerialTransport(create_real_config(), MagicMock(), MagicMock())
    with patch("serial.Serial", side_effect=OSError(errno.ENOTTY, "PTY")):
        transport._blocking_reset()
    with patch("serial.Serial", side_effect=OSError(errno.EIO, "Error")):
        transport._blocking_reset()

@pytest.mark.asyncio
async def test_serial_transport_run_loop_transport_closing():
    service = MagicMock()
    service.on_serial_connected = AsyncMock()
    service.on_serial_disconnected = AsyncMock()
    transport = SerialTransport(create_real_config(), MagicMock(), service)
    mock_transport = MagicMock()
    mock_transport.is_closing.return_value = True
    with (
        patch.object(transport, "_toggle_dtr", new_callable=AsyncMock),
        patch("mcubridge.transport.serial.serial_asyncio_fast.create_serial_connection", new_callable=AsyncMock) as mock_connect,
    ):
        mock_proto = MagicMock()
        mock_proto.connected_future = asyncio.get_running_loop().create_future()
        mock_proto.connected_future.set_result(None)
        mock_connect.return_value = (mock_transport, mock_proto)
        with pytest.raises(ConnectionError, match="lost"):
            await transport._connect_and_run(asyncio.get_running_loop())

# --- Process Booster ---

@pytest.mark.asyncio
async def test_process_handle_run_limit_reached():
    config = create_real_config()
    state = create_real_state()
    ctx = MagicMock()
    ctx.send_frame = AsyncMock()
    comp = ProcessComponent(config, state, ctx)
    comp._process_slots = MagicMock()
    comp._process_slots.acquire = AsyncMock(side_effect=asyncio.TimeoutError)
    
    from mcubridge.protocol.structures import ProcessRunPacket
    payload = ProcessRunPacket(command="ls").encode()
    await comp.handle_run(payload)
    assert ctx.send_frame.called

@pytest.mark.asyncio
async def test_process_execute_sync_oserror():
    comp = ProcessComponent(create_real_config(), create_real_state(), MagicMock())
    comp.run_sync = AsyncMock(side_effect=OSError("Sync Fail"))
    ctx = MagicMock()
    ctx.send_frame = AsyncMock()
    comp.ctx = ctx
    await comp._execute_sync_command("ls", ["ls"])
    assert ctx.send_frame.called

@pytest.mark.asyncio
async def test_process_handle_kill_not_found():
    ctx = MagicMock()
    ctx.send_frame = AsyncMock()
    comp = ProcessComponent(create_real_config(), create_real_state(), ctx)
    from mcubridge.protocol.structures import ProcessKillPacket
    payload = ProcessKillPacket(pid=999).encode()
    await comp.handle_kill(payload)
    assert ctx.send_frame.called

@pytest.mark.asyncio
async def test_process_run_sync_group_oserror():
    comp = ProcessComponent(create_real_config(), create_real_state(), MagicMock())
    mock_proc = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()
    mock_proc.wait = AsyncMock()
    
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with patch("mcubridge.services.process.ProcessComponent._consume_stream", side_effect=OSError("IO Error")):
            res = await comp.run_sync("ls", ["ls"])
            assert res[0] == Status.ERROR.value

@pytest.mark.asyncio
async def test_process_wait_for_sync_completion_timeout_kill_fail():
    comp = ProcessComponent(create_real_config(), create_real_state(), MagicMock())
    comp.state.process_timeout = 0.001
    mock_proc = MagicMock()
    mock_proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
    
    with patch("mcubridge.services.process.asyncio.timeout", side_effect=TimeoutError):
        res = await comp._wait_for_sync_completion(mock_proc, 123)
        assert res is True

@pytest.mark.asyncio
async def test_process_allocate_pid_exhaustion():
    state = create_real_state()
    state.running_processes = {i: MagicMock() for i in range(1, 65536)}
    comp = ProcessComponent(create_real_config(), state, MagicMock())
    pid = await comp._allocate_pid()
    assert pid == INVALID_ID_SENTINEL

@pytest.mark.asyncio
async def test_process_terminate_tree_no_pid():
    comp = ProcessComponent(create_real_config(), create_real_state(), MagicMock())
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.pid = None
    await comp._terminate_process_tree(mock_proc)
    assert mock_proc.kill.called

@pytest.mark.asyncio
async def test_process_finalize_async_replaced_slot():
    state = create_real_state()
    pid = 123
    slot1 = MagicMock()
    slot1.io_lock = asyncio.Lock()
    state.running_processes = MagicMock()
    state.running_processes.get.return_value = slot1
    comp = ProcessComponent(create_real_config(), state, MagicMock())
    
    mock_proc = MagicMock()
    mock_proc.stdout.read = AsyncMock(return_value=b"")
    mock_proc.stderr.read = AsyncMock(return_value=b"")
    mock_proc.returncode = 0
    
    slot2 = MagicMock()
    with patch.object(state.running_processes, "get", return_value=slot2):
        await comp._finalize_async_process(pid, mock_proc)

@pytest.mark.asyncio
async def test_process_kill_tree_sync_errors():
    with patch("psutil.Process", side_effect=psutil.NoSuchProcess(123)):
        ProcessComponent._kill_process_tree_sync(123)
    
    mock_p = MagicMock()
    mock_p.children.side_effect = psutil.AccessDenied()
    with patch("psutil.Process", return_value=mock_p):
        ProcessComponent._kill_process_tree_sync(123)

# --- Handshake Booster ---

def test_handshake_timing_seconds():
    tw = SerialTimingWindow(ack_timeout_ms=100, response_timeout_ms=500, retry_limit=3)
    assert tw.ack_timeout_seconds == 0.1
    assert tw.response_timeout_seconds == 0.5

def test_handshake_retry_helpers():
    assert _retry_if_false(False) is True
    assert _retry_if_false(True) is False
    
    mock_rs = MagicMock()
    mock_rs.attempt_number = 1
    mock_rs.next_action.sleep = 0.5
    _log_handshake_retry(mock_rs)

@pytest.mark.asyncio
async def test_handshake_manager_synchronize_failure():
    cfg = create_real_config()
    cfg.serial_handshake_fatal_failures = 1
    state = create_real_state()
    timing = derive_serial_timing(cfg)
    
    # Mock send_frame to always fail
    h = SerialHandshakeManager(
        config=cfg,
        state=state,
        serial_timing=timing,
        send_frame=AsyncMock(return_value=False),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    
    # Low threshold for fast test
    with patch.object(h, "_fatal_threshold", 1):
        res = await h.synchronize()
        assert res is False
        assert h.fsm_state == h.STATE_FAULT

@pytest.mark.asyncio
async def test_handshake_handle_link_sync_resp_throttled():
    cfg = create_real_config()
    cfg.serial_handshake_min_interval = 10.0
    state = create_real_state()
    state.handshake_rate_limit_until = time.monotonic() + 5.0
    state.link_handshake_nonce = b"12345678"
    
    h = SerialHandshakeManager(
        config=cfg,
        state=state,
        serial_timing=derive_serial_timing(cfg),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    
    res = await h.handle_link_sync_resp(b"anything")
    assert res is False

@pytest.mark.asyncio
async def test_handshake_handle_link_sync_resp_malformed():
    state = create_real_state()
    state.link_handshake_nonce = b"12345678" # 8 bytes
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=state,
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    # Required length = 8 + 16 = 24
    res = await h.handle_link_sync_resp(b"too_short")
    assert res is False

@pytest.mark.asyncio
async def test_handshake_handle_link_sync_resp_auth_mismatch():
    state = create_real_state()
    nonce = b"12345678"
    state.link_handshake_nonce = nonce
    state.link_nonce_length = 8
    state.link_expected_tag = b"correct_tag_16by"
    
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=state,
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    
    # Required length = 8 + 16 = 24. We provide WRONG tag.
    bad_payload = nonce + b"wrong_tag_16byte"
    res = await h.handle_link_sync_resp(bad_payload)
    assert res is False

@pytest.mark.asyncio
async def test_handshake_handle_link_sync_resp_replay():
    state = create_real_state()
    nonce = b"12345678"
    state.link_handshake_nonce = nonce
    state.link_nonce_length = 8
    # We need a tag that matches
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=state,
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    tag = h.compute_handshake_tag(nonce)
    state.link_expected_tag = tag
    
    # Set high last counter to trigger replay
    state.link_last_nonce_counter = 999999
    # Handshake counter in nonce is 0 (from generate_nonce_with_counter(0))
    res = await h.handle_link_sync_resp(nonce + tag)
    assert res is False

@pytest.mark.asyncio
async def test_handshake_handle_link_sync_resp_no_expected_tag():
    state = create_real_state()
    nonce = b"12345678"
    state.link_handshake_nonce = nonce
    state.link_nonce_length = 8
    state.link_expected_tag = None # Missing expected tag
    
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=state,
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    tag = h.compute_handshake_tag(nonce)
    res = await h.handle_link_sync_resp(nonce + tag)
    assert res is False

@pytest.mark.asyncio
async def test_handshake_synchronize_attempt_race_fault():
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(return_value=True),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    # We want to trigger line 286: if self.fsm_state == self.STATE_FAULT: return False
    # after start_sync() but before start_confirm().
    # We can patch start_confirm to set state to fault first.
    with patch.object(h, "start_confirm", side_effect=h.fail_handshake):
        res = await h._synchronize_attempt()
        # It will return False at line 286 if we trigger it right after start_sync.
        # Wait, start_confirm is AFTER the check at 286.
        # Check at 286 is BEFORE start_confirm.
        
    # Let's patch start_sync to also fail_handshake
    with patch.object(h, "start_sync", side_effect=h.fail_handshake):
        res = await h._synchronize_attempt()
        assert res is False
        assert h.fsm_state == h.STATE_FAULT

@pytest.mark.asyncio
async def test_handshake_synchronize_attempt_timeout():
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(return_value=True),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    # Ensure nonce is set so it doesn't return True at the end
    h._state.link_handshake_nonce = b"1234"
    with patch.object(h, "_wait_for_link_sync_confirmation", return_value=False):
        res = await h._synchronize_attempt()
        assert res is False

@pytest.mark.asyncio
async def test_handshake_synchronize_attempt_nonce_changed():
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(return_value=True),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    # Mock confirm to return False and CHANGE nonce in the middle
    async def side_effect(*args):
        h._state.link_handshake_nonce = b"CHANGED"
        return False
    
    with patch.object(h, "_wait_for_link_sync_confirmation", side_effect=side_effect):
        res = await h._synchronize_attempt()
        assert res is False

@pytest.mark.asyncio
async def test_handshake_handle_link_sync_resp_no_nonce():
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    res = await h.handle_link_sync_resp(b"anything")
    assert res is False

@pytest.mark.asyncio
async def test_handshake_handle_link_sync_resp_length_mismatch():
    state = create_real_state()
    state.link_handshake_nonce = b"12345678"
    state.link_nonce_length = 8
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=state,
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    # Wrong length (expected 8+16=24)
    res = await h.handle_link_sync_resp(b"too_short")
    assert res is False

@pytest.mark.asyncio
async def test_handshake_handle_link_sync_resp_rate_limited():
    cfg = create_real_config()
    cfg.serial_handshake_min_interval = 100.0
    state = create_real_state()
    state.link_handshake_nonce = b"12345678"
    state.handshake_rate_limit_until = time.monotonic() + 50.0
    
    h = SerialHandshakeManager(
        config=cfg,
        state=state,
        serial_timing=derive_serial_timing(cfg),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    res = await h.handle_link_sync_resp(b"anything")
    assert res is False

@pytest.mark.asyncio
async def test_handshake_fetch_capabilities_send_fail():
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(return_value=False),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    with patch("tenacity.nap.time.sleep"):
        res = await h._fetch_capabilities()
        assert res is False

@pytest.mark.asyncio
async def test_handshake_fetch_capabilities_timeout():
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(return_value=True),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    with (
        patch("asyncio.wait_for", side_effect=asyncio.TimeoutError),
        patch("tenacity.nap.time.sleep")
    ):
        res = await h._fetch_capabilities()
        assert res is False

def test_handshake_parse_capabilities_error():
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    with patch("mcubridge.protocol.structures.CapabilitiesPacket.decode", side_effect=ValueError("Bad")):
        h._parse_capabilities(b"garbage")

@pytest.mark.asyncio
async def test_handshake_handle_capabilities_resp_no_future():
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    h._capabilities_future = None
    res = await h.handle_capabilities_resp(b"data")
    assert res is True

def test_handshake_calculate_tag_empty():
    assert SerialHandshakeManager.calculate_handshake_tag(None, b"nonce") == b""
    assert SerialHandshakeManager.calculate_handshake_tag(b"", b"nonce") == b""

def test_handshake_calculate_tag_debug_insecure():
    res = SerialHandshakeManager.calculate_handshake_tag(b"DEBUG_INSECURE", b"nonce")
    assert res == b"DEBUG_TAG_UNUSED"

def test_handshake_maybe_schedule_backoff_fatal():
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    h._state.handshake_failure_streak = 1
    delay = h._maybe_schedule_handshake_backoff("sync_auth_mismatch")
    assert delay is not None
    assert h._state.handshake_backoff_until > time.monotonic()

def test_handshake_maybe_schedule_backoff_streak():
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    h._state.handshake_failure_streak = 3
    delay = h._maybe_schedule_handshake_backoff("other_reason")
    assert delay is not None

@pytest.mark.asyncio
async def test_handshake_handle_failure_immediate_fatal():
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    # "sync_auth_mismatch" is in _IMMEDIATE_FATAL_HANDSHAKE_REASONS
    await h.handle_handshake_failure("sync_auth_mismatch", detail="auth_fail")
    assert h._state.handshake_fatal_count == 1
    assert h._state.handshake_fatal_reason == "sync_auth_mismatch"

def test_handshake_raise_if_fatal():
    from mcubridge.services.handshake import SerialHandshakeFatal
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    h._state.handshake_fatal_reason = "sync_auth_mismatch"
    with pytest.raises(SerialHandshakeFatal):
        h.raise_if_handshake_fatal()

def test_handshake_raise_if_fatal_none():
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    h._state.handshake_fatal_reason = None
    h.raise_if_handshake_fatal() # Should not raise

@pytest.mark.asyncio
async def test_handshake_handle_link_sync_resp_no_nonce_complete():
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    h._state.link_handshake_nonce = None
    res = await h.handle_link_sync_resp(b"data")
    assert res is False

def test_handshake_backoff_remaining():
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    h._state.handshake_backoff_until = time.monotonic() + 100
    assert h._handshake_backoff_remaining() > 0
    h._state.handshake_backoff_until = 0
    assert h._handshake_backoff_remaining() == 0

def test_handshake_clear_expectations_full():
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    h._state.link_handshake_nonce = b"nonce"
    h._state.link_expected_tag = b"tag"
    h.clear_handshake_expectations()
    assert h._state.link_handshake_nonce is None
    assert h._state.link_expected_tag is None

@pytest.mark.asyncio
async def test_handshake_synchronize_retry_error():
    h = SerialHandshakeManager(
        config=create_real_config(),
        state=create_real_state(),
        serial_timing=derive_serial_timing(create_real_config()),
        send_frame=AsyncMock(),
        enqueue_mqtt=AsyncMock(),
        acknowledge_frame=AsyncMock()
    )
    # Force RetryError by making _synchronize_attempt raise it OR exhausting it
    with patch.object(h, "_synchronize_attempt", side_effect=tenacity.RetryError(last_attempt=MagicMock())):
        res = await h.synchronize()
        assert res is False
        assert h.fsm_state == h.STATE_FAULT
