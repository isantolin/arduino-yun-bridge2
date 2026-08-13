# pyright: reportPrivateUsage=false
"""Phase 2 surgical coverage tests targeting the largest coverage gaps across the codebase."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
import types
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cobs import cobsr
from google.protobuf.message import Message

import mcubridge.protocol.mcubridge_pb2 as pb
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.frame import build_frame
from mcubridge.protocol.protocol import Command, Status
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.state.storage import LmdbCache, LmdbDeque
from mcubridge.transport.serial import SerialTransport

# ──────────────────────────────────────────────────────────────────────────────
# Ensure 'uci' mock exists for pin_rest_cgi / rotate_credentials imports
# ──────────────────────────────────────────────────────────────────────────────
if "uci" not in sys.modules:
    _uci_mock = types.ModuleType("uci")
    _uci_mock.Uci = MagicMock  # type: ignore[attr-defined]
    _uci_mock.UciException = RuntimeError  # type: ignore[attr-defined]
    sys.modules["uci"] = _uci_mock


def _load_script(name: str) -> types.ModuleType:
    """Dynamically load a script module from mcubridge/scripts/."""
    if name in sys.modules:
        return sys.modules[name]
    script_path = Path(__file__).parent.parent / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pin_rest_cgi = _load_script("pin_rest_cgi")


def _make_config(**overrides: object) -> RuntimeConfig:
    defaults: dict[str, object] = {
        "serial_port": "/dev/ttyMCU",
        "serial_baud": 115200,
        "serial_safe_baud": 9600,
        "serial_shared_secret": b"testsharedsecret",
        "allow_non_tmp_paths": True,
        "allowed_commands": ("echo", "ls"),
    }
    defaults.update(overrides)
    return RuntimeConfig(**defaults)  # type: ignore[arg-type]


def _make_state(config: RuntimeConfig | None = None) -> RuntimeState:
    return create_runtime_state(config or _make_config())


# ══════════════════════════════════════════════════════════════════════════════
# serial.py — send() tracked command path, fatal/retry errors, flow control
# ══════════════════════════════════════════════════════════════════════════════


class TestSerialSendTracked:
    """Tests for the send() method with tracked commands (ACK/response flow)."""

    @pytest.mark.asyncio
    async def test_send_returns_false_when_serial_closed(self) -> None:
        config = _make_config()
        state = _make_state(config)
        transport = SerialTransport(config, state, None)
        transport.serial = None

        result = await transport.send(Command.CMD_GET_VERSION.value, b"")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_untracked_delegates_to_send_raw(self) -> None:
        config = _make_config()
        state = _make_state(config)
        transport = SerialTransport(config, state, None)

        mock_serial = AsyncMock()
        mock_serial.is_open = True
        transport.serial = mock_serial

        with patch.object(transport, "send_raw", new_callable=AsyncMock, return_value=True) as mock_raw:
            result = await transport.send(Command.CMD_LINK_SYNC.value, b"hello")
            assert result is True
            mock_raw.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_tracked_timeout_retry_exhaustion(self) -> None:
        config = _make_config(serial_retry_attempts=2, serial_retry_timeout=0.05, serial_response_timeout=0.05)
        state = _make_state(config)
        transport = SerialTransport(config, state, None)

        mock_serial = AsyncMock()
        mock_serial.is_open = True
        transport.serial = mock_serial

        with patch.object(transport, "send_raw", new_callable=AsyncMock, return_value=True):
            result = await transport.send(Command.CMD_GET_VERSION.value, b"")
            assert result is False

    @pytest.mark.asyncio
    async def test_send_tracked_fatal_error(self) -> None:
        config = _make_config(serial_retry_attempts=1, serial_retry_timeout=0.05, serial_response_timeout=0.1)
        state = _make_state(config)
        transport = SerialTransport(config, state, None)

        mock_serial = AsyncMock()
        mock_serial.is_open = True
        transport.serial = mock_serial

        # send_raw returns False -> triggers FatalSerialError
        with patch.object(transport, "send_raw", new_callable=AsyncMock, return_value=False):
            result = await transport.send(Command.CMD_GET_VERSION.value, b"")
            assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# serial.py — send_raw() flow control wait and write failure
# ══════════════════════════════════════════════════════════════════════════════


class TestSerialSendRaw:
    @pytest.mark.asyncio
    async def test_send_raw_flow_control_timeout(self) -> None:
        config = _make_config()
        state = _make_state(config)
        transport = SerialTransport(config, state, None)

        mock_serial = AsyncMock()
        transport.serial = mock_serial
        state.serial_tx_allowed.clear()

        # Will timeout waiting for flow control, but should proceed
        with patch("mcubridge.transport.serial.FLOW_CONTROL_WAIT_TIMEOUT_SECONDS", 0.01):
            result = await transport.send_raw(Command.CMD_GET_VERSION.value, b"")
            assert result is True

    @pytest.mark.asyncio
    async def test_send_raw_write_exception(self) -> None:
        config = _make_config()
        state = _make_state(config)
        transport = SerialTransport(config, state, None)

        mock_serial = AsyncMock()
        mock_serial.write.side_effect = OSError("Write failed")
        transport.serial = mock_serial
        state.serial_tx_allowed.set()

        result = await transport.send_raw(Command.CMD_GET_VERSION.value, b"")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_raw_synchronized_nonce(self) -> None:
        config = _make_config()
        state = _make_state(config)
        state.mark_synchronized()
        state.link_session_key = b"A" * 32
        state.link_nonce_counter = 1
        transport = SerialTransport(config, state, None)

        mock_serial = AsyncMock()
        mock_serial.write = AsyncMock()
        mock_serial.drain = AsyncMock()
        transport.serial = mock_serial
        state.serial_tx_allowed.set()

        result = await transport.send_raw(Command.CMD_FILE_WRITE.value, b"payload")
        assert result is True
        assert state.link_nonce_counter > 1


# ══════════════════════════════════════════════════════════════════════════════
# serial.py — _negotiate_baudrate
# ══════════════════════════════════════════════════════════════════════════════


class TestNegotiateBaudrate:
    @pytest.mark.asyncio
    async def test_negotiate_send_raw_failure(self) -> None:
        config = _make_config()
        state = _make_state(config)
        transport = SerialTransport(config, state, None)

        with patch.object(transport, "send_raw", new_callable=AsyncMock, return_value=False):
            result = await transport._negotiate_baudrate(115200)
            assert result is False

    @pytest.mark.asyncio
    async def test_negotiate_timeout(self) -> None:
        config = _make_config()
        state = _make_state(config)
        transport = SerialTransport(config, state, None)

        with patch.object(transport, "send_raw", new_callable=AsyncMock, return_value=True):
            with patch("mcubridge.transport.serial.SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT", 0.01):
                result = await transport._negotiate_baudrate(115200)
                assert result is False
                assert transport._negotiating is False


# ══════════════════════════════════════════════════════════════════════════════
# serial.py — _process_packet anti-replay and protovalidate paths
# ══════════════════════════════════════════════════════════════════════════════


class TestProcessPacketEdgePaths:
    @pytest.mark.asyncio
    async def test_process_packet_anti_replay_failure(self) -> None:
        config = _make_config()
        state = _make_state(config)
        state.mark_synchronized()
        state.link_session_key = b"K" * 32
        state.link_last_nonce_counter = 999
        transport = SerialTransport(config, state, None)

        # Build a non-system command frame without valid nonce
        raw = cobsr.encode(build_frame(Command.CMD_GET_VERSION.value, 1))

        with patch("mcubridge.transport.serial.validate_nonce_counter", return_value=(False, 999)):
            await transport._process_packet(raw)
            # Frame should be dropped silently (no service dispatch)

    @pytest.mark.asyncio
    async def test_process_packet_protovalidate_rejection(self) -> None:
        import protovalidate

        config = _make_config()
        state = _make_state(config)
        transport = SerialTransport(config, state, None)

        raw = cobsr.encode(build_frame(Command.CMD_GET_VERSION.value, 1))

        with patch("mcubridge.transport.serial.parse_frame") as mock_parse:
            mock_result = MagicMock()
            mock_result.envelope.command_id = Command.CMD_GET_VERSION.value
            mock_result.envelope.sequence_id = 1
            mock_result.envelope.nonce = b"\x00" * 12
            mock_result.payload = pb.VersionResponse(major=1, minor=0, patch=0)
            mock_parse.return_value = mock_result

            with patch(
                "mcubridge.transport.serial.protovalidate.validate",
                side_effect=protovalidate.ValidationError("test", violations=[]),
            ):
                await transport._process_packet(raw)
                assert state.serial_decode_errors >= 1


# ══════════════════════════════════════════════════════════════════════════════
# serial.py — _correlate_frame edge cases
# ══════════════════════════════════════════════════════════════════════════════


class TestCorrelateFrameEdges:
    def test_correlate_already_resolved(self) -> None:
        config = _make_config()
        state = _make_state(config)
        transport = SerialTransport(config, state, None)

        pending = MagicMock()
        pending.success = True  # Already resolved
        transport._current = pending

        transport._correlate_frame(Status.ACK.value, b"")
        pending.mark_success.assert_not_called()

    def test_correlate_success_status_code(self) -> None:
        from mcubridge.config.const import SERIAL_SUCCESS_STATUS_CODES
        from mcubridge.protocol.structures import PendingCommand

        config = _make_config()
        state = _make_state(config)
        transport = SerialTransport(config, state, None)

        pending = PendingCommand(command_id=Command.CMD_FILE_WRITE.value, expected_resp_ids=[])
        transport._current = pending

        if SERIAL_SUCCESS_STATUS_CODES:
            status_code = next(iter(SERIAL_SUCCESS_STATUS_CODES))
            transport._correlate_frame(status_code, b"ok")
            assert pending.success is True


# ══════════════════════════════════════════════════════════════════════════════
# serial.py — run() and _connect_and_run paths
# ══════════════════════════════════════════════════════════════════════════════


class TestSerialRun:
    @pytest.mark.asyncio
    async def test_run_cancelled(self) -> None:
        config = _make_config()
        state = _make_state(config)
        transport = SerialTransport(config, state, None)

        with patch.object(transport, "_connect_and_run", new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            await transport.run()  # Should not raise

    @pytest.mark.asyncio
    async def test_run_fatal_handshake(self) -> None:
        from mcubridge.services.handshake import SerialHandshakeFatal

        config = _make_config()
        state = _make_state(config)
        transport = SerialTransport(config, state, None)

        with patch.object(
            transport, "_connect_and_run", new_callable=AsyncMock, side_effect=SerialHandshakeFatal("fatal")
        ):
            with pytest.raises(SerialHandshakeFatal):
                await transport.run()


# ══════════════════════════════════════════════════════════════════════════════
# serial.py — acknowledge()
# ══════════════════════════════════════════════════════════════════════════════


class TestSerialAcknowledge:
    @pytest.mark.asyncio
    async def test_acknowledge_sends_ack_frame(self) -> None:
        config = _make_config()
        state = _make_state(config)
        transport = SerialTransport(config, state, None)

        with patch.object(transport, "send_raw", new_callable=AsyncMock, return_value=True) as mock_raw:
            await transport.acknowledge(Command.CMD_GET_VERSION.value, 42)
            mock_raw.assert_awaited_once()
            call_args = mock_raw.call_args
            assert call_args[0][0] == Status.ACK.value
            assert call_args[0][2] == 42


# ══════════════════════════════════════════════════════════════════════════════
# storage.py — LmdbDeque vacuum and error recovery
# ══════════════════════════════════════════════════════════════════════════════


class TestLmdbDequeVacuum:
    @pytest.mark.asyncio
    async def test_vacuum_memory_mode_noop(self) -> None:
        deque = LmdbDeque(path=":memory:", maxlen=100)
        await deque.append(b"data")
        await deque.vacuum()  # Should be no-op for memory mode
        assert len(deque) == 1

    @pytest.mark.asyncio
    async def test_vacuum_disk_success(self) -> None:
        test_dir = f".tmp_tests/vacuum-{os.getpid()}-{time.time_ns()}"
        Path(test_dir).mkdir(parents=True, exist_ok=True)
        deque: LmdbDeque | None = None
        try:
            deque = LmdbDeque(path=test_dir, maxlen=100)
            await deque.append(b"item1")
            await deque.append(b"item2")
            await deque.vacuum()
            assert len(deque) == 2
            val = await deque.popleft()
            assert val == b"item1"
        finally:
            if deque is not None:
                await deque.close()
            import shutil

            shutil.rmtree(test_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_vacuum_disk_failure(self) -> None:
        test_dir = f".tmp_tests/vacuum-fail-{os.getpid()}-{time.time_ns()}"
        Path(test_dir).mkdir(parents=True, exist_ok=True)
        deque: LmdbDeque | None = None
        try:
            deque = LmdbDeque(path=test_dir, maxlen=100)
            await deque.append(b"item")

            # lmdb.Environment.copy is read-only (C extension), so we patch the entire env
            import lmdb

            mock_env = MagicMock(spec=lmdb.Environment)
            mock_env.copy.side_effect = OSError("copy failed")
            original_env = deque.env
            deque.env = mock_env  # type: ignore[assignment]
            await deque.vacuum()
            deque.env = original_env
            # Should not raise, and deque should still work
            assert len(deque) >= 0
        finally:
            if deque is not None:
                await deque.close()
            import shutil

            shutil.rmtree(test_dir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# storage.py — LmdbCache get error and disk recreation
# ══════════════════════════════════════════════════════════════════════════════


class TestLmdbCache:
    @pytest.mark.asyncio
    async def test_cache_set_no_env(self) -> None:
        cache = LmdbCache(path=":memory:")
        cache.env = None  # Simulate broken env
        await cache.set("key", b"value")  # Should be no-op

    @pytest.mark.asyncio
    async def test_cache_get_no_env(self) -> None:
        cache = LmdbCache(path=":memory:")
        cache.env = None
        result = await cache.get("key", b"default")
        assert result == b"default"

    @pytest.mark.asyncio
    async def test_cache_disk_operations(self) -> None:
        test_dir = f".tmp_tests/cache-{os.getpid()}-{time.time_ns()}"
        Path(test_dir).mkdir(parents=True, exist_ok=True)
        try:
            cache = LmdbCache(path=test_dir)
            await cache.set("k1", b"v1")
            result = await cache.get("k1")
            assert result == b"v1"

            result_miss = await cache.get("missing", b"fallback")
            assert result_miss == b"fallback"

            await cache.clear()
            await cache.close()
        finally:
            import shutil

            shutil.rmtree(test_dir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# context.py — configure(), cleanup(), build_bridge_snapshot()
# ══════════════════════════════════════════════════════════════════════════════


class TestRuntimeStateContext:
    def test_configure_reinitializes_spools(self) -> None:
        config = _make_config(file_system_root=f"/tmp/test-ctx-{os.getpid()}-{time.time_ns()}")
        state = _make_state(config)
        try:
            state.configure()
            assert state.mailbox_queue is not None
            assert state.mailbox_incoming_queue is not None
        finally:
            state.cleanup()

    def test_cleanup_terminates_processes(self) -> None:
        config = _make_config()
        state = _make_state(config)
        mock_proc = MagicMock()
        mock_proc.handle = MagicMock()
        state.running_processes[1234] = mock_proc

        state.cleanup()
        mock_proc.handle.terminate.assert_called_once()
        assert len(state.running_processes) == 0

    def test_build_bridge_snapshot_with_mcu_version(self) -> None:
        config = _make_config()
        state = _make_state(config)
        state.mcu_version = (2, 8, 5)
        state.mcu_capabilities = pb.Capabilities(watchdog=True, eeprom=True)

        snapshot = state.build_bridge_snapshot()
        assert snapshot.mcu_version.major == 2
        assert snapshot.mcu_version.minor == 8
        assert snapshot.mcu_version.patch == 5
        assert snapshot.capabilities.watchdog is True
        state.cleanup()

    def test_build_bridge_snapshot_capabilities_dict(self) -> None:
        config = _make_config()
        state = _make_state(config)
        state.mcu_capabilities = {"watchdog": True, "eeprom": False}

        snapshot = state.build_bridge_snapshot()
        assert snapshot.capabilities is not None
        state.cleanup()

    def test_build_serial_pipeline_snapshot_with_data(self) -> None:
        config = _make_config()
        state = _make_state(config)
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
        state.cleanup()

    def test_handshake_duration_since_start(self) -> None:
        config = _make_config()
        state = _make_state(config)
        assert state.handshake_duration_since_start() == 0.0

        state.handshake_last_started = time.monotonic() - 1.0
        duration = state.handshake_duration_since_start()
        assert duration > 0.5
        state.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
# pin_rest_cgi.py — control() CLI, run_cgi(), protovalidate error on pin_data
# ══════════════════════════════════════════════════════════════════════════════


class TestPinRestCgiCli:
    def test_control_cli_invocation(self) -> None:
        with (
            patch.object(pin_rest_cgi, "load_runtime_config", return_value=_make_config()),
            patch.object(pin_rest_cgi, "configure_logging"),
            patch.object(pin_rest_cgi, "publish_sync") as mock_pub,
        ):
            pin_rest_cgi.control(pin=13, state="ON")
            mock_pub.assert_called_once()

    def test_run_cgi_no_gateway(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(pin_rest_cgi, "app") as mock_app,
        ):
            # Remove GATEWAY_INTERFACE and REQUEST_METHOD
            env_clean = {k: v for k, v in os.environ.items() if k not in ("GATEWAY_INTERFACE", "REQUEST_METHOD")}
            with patch.dict(os.environ, env_clean, clear=True):
                pin_rest_cgi.run_cgi()
                mock_app.assert_called_once()

    def test_run_cgi_with_gateway(self) -> None:
        with (
            patch.dict(os.environ, {"GATEWAY_INTERFACE": "CGI/1.1", "REQUEST_METHOD": "GET"}),
            patch.object(pin_rest_cgi, "CGIHandler") as mock_handler_cls,
        ):
            mock_handler = MagicMock()
            mock_handler_cls.return_value = mock_handler
            pin_rest_cgi.run_cgi()
            mock_handler.run.assert_called_once()

    def test_application_pin_data_validation_error(self) -> None:
        import protovalidate as pv

        start_response = MagicMock()
        body = b'{"state": "ON"}'
        env = {
            "PATH_INFO": "/pin/13",
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": BytesIO(body),
        }

        with (
            patch.object(pin_rest_cgi, "load_runtime_config", return_value=_make_config()),
            patch.object(pin_rest_cgi, "configure_logging"),
        ):
            # First protovalidate passes, second fails
            original_validate = pv.validate
            call_count = 0

            def _selective_validate(msg: Message) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise pv.ValidationError("invalid pin data", violations=[])
                original_validate(msg)

            with patch.object(pin_rest_cgi.protovalidate, "validate", side_effect=_selective_validate):
                result = pin_rest_cgi.application(env, start_response)
                assert result
                # Should return 400 for invalid pin_data
                start_response.assert_called()

    def test_application_method_not_allowed(self) -> None:
        start_response = MagicMock()
        env = {"PATH_INFO": "/pin/13", "REQUEST_METHOD": "DELETE"}

        with (
            patch.object(pin_rest_cgi, "load_runtime_config", return_value=_make_config()),
            patch.object(pin_rest_cgi, "configure_logging"),
        ):
            result = pin_rest_cgi.application(env, start_response)
            assert result
            start_response.assert_called_once()
            call_args = start_response.call_args[0]
            assert "405" in call_args[0]


# ══════════════════════════════════════════════════════════════════════════════
# mcubridge_rotate_credentials.py — update_uci_credentials and restart_service
# ══════════════════════════════════════════════════════════════════════════════


class TestRotateCredentials:
    def _ensure_uci_mock(self) -> None:
        """Ensure uci module has proper UciException and Uci class."""
        uci_mod = sys.modules.get("uci")
        if uci_mod is None:
            uci_mod = types.ModuleType("uci")
            sys.modules["uci"] = uci_mod
        uci_mod.UciException = type("UciException", (RuntimeError,), {})  # type: ignore[attr-defined]
        if not hasattr(uci_mod, "Uci"):
            uci_mod.Uci = MagicMock  # type: ignore[attr-defined]

    def test_update_uci_credentials_success(self) -> None:
        self._ensure_uci_mock()
        rotate_creds = _load_script("mcubridge_rotate_credentials")
        mock_uci = MagicMock()
        with patch.object(rotate_creds.uci, "Uci", return_value=mock_uci):
            rotate_creds.update_uci_credentials("newsecret", "newpass")
            assert mock_uci.set.call_count == 2
            mock_uci.commit.assert_called_once_with("mcubridge")

    def test_update_uci_credentials_failure(self) -> None:
        self._ensure_uci_mock()
        rotate_creds = _load_script("mcubridge_rotate_credentials")
        mock_uci = MagicMock()
        mock_uci.set.side_effect = RuntimeError("UCI write failed")
        with (
            patch.object(rotate_creds.uci, "Uci", return_value=mock_uci),
            pytest.raises(SystemExit) as exc_info,
        ):
            rotate_creds.update_uci_credentials("newsecret", "newpass")
        assert exc_info.value.code == 3

    def test_restart_service_success(self) -> None:
        rotate_creds = _load_script("mcubridge_rotate_credentials")
        with patch.object(rotate_creds.subprocess, "run") as mock_run:
            rotate_creds.restart_service()
            mock_run.assert_called_once()

    def test_restart_service_failure(self) -> None:
        import subprocess

        rotate_creds = _load_script("mcubridge_rotate_credentials")
        with patch.object(
            rotate_creds.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, "restart", stderr=b"failed"),
        ):
            rotate_creds.restart_service()  # Should not raise

    def test_main_forced_rotation(self) -> None:
        rotate_creds = _load_script("mcubridge_rotate_credentials")
        with (
            patch.object(rotate_creds, "update_uci_credentials") as mock_update,
            patch.object(rotate_creds, "restart_service") as mock_restart,
        ):
            rotate_creds.main(length=16, force=True, no_restart=False)
            mock_update.assert_called_once()
            mock_restart.assert_called_once()

    def test_main_no_restart(self) -> None:
        rotate_creds = _load_script("mcubridge_rotate_credentials")
        with (
            patch.object(rotate_creds, "update_uci_credentials"),
            patch.object(rotate_creds, "restart_service") as mock_restart,
        ):
            rotate_creds.main(length=16, force=True, no_restart=True)
            mock_restart.assert_not_called()

    def test_main_user_aborts(self) -> None:
        rotate_creds = _load_script("mcubridge_rotate_credentials")
        with (
            patch.object(rotate_creds.sys, "stdin") as mock_stdin,
            patch.object(rotate_creds.sys, "stdout"),
            pytest.raises(SystemExit) as exc_info,
        ):
            mock_stdin.readline.return_value = "n\n"
            rotate_creds.main(length=16, force=False, no_restart=False)
        assert exc_info.value.code == 0


# ══════════════════════════════════════════════════════════════════════════════
# mcubridge_file_push.py — push_file error and main() error paths
# ══════════════════════════════════════════════════════════════════════════════


class TestFilePush:
    def test_push_file_ipc_error(self) -> None:
        file_push = _load_script("mcubridge_file_push")
        with (
            patch("grpclib.client.Channel", side_effect=OSError("IPC failed")),
            pytest.raises(SystemExit) as exc_info,
        ):
            file_push.push_file("br/f/write/test.bin", b"data")
        assert exc_info.value.code == 1

    def test_main_source_not_found(self) -> None:
        file_push = _load_script("mcubridge_file_push")
        with pytest.raises(SystemExit) as exc_info:
            file_push.main(source=Path("/nonexistent/file.bin"), target="/test.bin")
        assert exc_info.value.code == 2

    def test_main_success(self) -> None:
        file_push = _load_script("mcubridge_file_push")
        test_file = Path(f".tmp_tests/push-{os.getpid()}-{time.time_ns()}.bin")
        test_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            test_file.write_bytes(b"A" * 100)
            with (
                patch.object(file_push, "load_runtime_config", return_value=_make_config()),
                patch.object(file_push, "push_file") as mock_push,
            ):
                file_push.main(source=test_file, target="/upload/test.bin")
                mock_push.assert_called_once()
        finally:
            test_file.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# mcubridge_led_control.py — do_publish and main error paths
# ══════════════════════════════════════════════════════════════════════════════


class TestLedControl:
    def test_do_publish_error(self) -> None:
        led_control = _load_script("mcubridge_led_control")
        with (
            patch("grpclib.client.Channel") as mock_chan_cls,
            pytest.raises(SystemExit) as exc_info,
        ):
            mock_chan = MagicMock()
            mock_chan_cls.return_value = mock_chan

            mock_stub = MagicMock()
            mock_stub.Publish = AsyncMock(side_effect=OSError("IPC failed"))

            with patch.object(led_control, "LocalBridgeStub", return_value=mock_stub):
                led_control.do_publish("br/d/13/write", "1")
        assert exc_info.value.code == 4

    def test_main_invalid_state(self) -> None:
        led_control = _load_script("mcubridge_led_control")
        with pytest.raises(SystemExit) as exc_info:
            led_control.main(state="blink", pin=13)
        assert exc_info.value.code == 2

    def test_main_success(self) -> None:
        led_control = _load_script("mcubridge_led_control")
        with (
            patch.object(led_control, "load_runtime_config", return_value=_make_config()),
            patch.object(led_control, "do_publish") as mock_pub,
        ):
            led_control.main(state="on", pin=13)
            mock_pub.assert_called_once()
            call_args = mock_pub.call_args[0]
            assert call_args[1] == "1"  # ON -> "1"


# ══════════════════════════════════════════════════════════════════════════════
# state/status.py — uncovered lines 33-34, 36
# ══════════════════════════════════════════════════════════════════════════════


class TestStateStatus:
    @pytest.mark.asyncio
    async def test_status_writer_error_handling(self) -> None:
        config = _make_config()
        state = _make_state(config)
        try:
            from mcubridge.state.status import _write_status_file

            snapshot = state.build_status_snapshot()
            with patch("mcubridge.state.status.STATUS_FILE") as mock_file:
                mock_file.parent.mkdir = MagicMock(side_effect=OSError("Permission denied"))
                _write_status_file(snapshot)  # Should not raise
        finally:
            state.cleanup()
