"""Third targeted coverage gap closure for scripts, pin_rest_cgi, rotate_credentials,
led_control, and runtime.py spool/message handling. [SIL-2]"""

from __future__ import annotations

import asyncio
import importlib.util
from io import BytesIO
from pathlib import Path
import sys
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport


# Dynamic script imports
def _load_script(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).parent.parent / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sys.modules["uci"] = MagicMock()

pin_rest_cgi = _load_script("pin_rest_cgi")
mcubridge_rotate_credentials = _load_script("mcubridge_rotate_credentials")
mcubridge_led_control = _load_script("mcubridge_led_control")


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
# pin_rest_cgi.py (77% -> 100%)
# ==============================================================================


def test_pin_rest_cgi_invalid_path() -> None:
    """400 Bad Request on invalid path format."""
    start_response = MagicMock()
    env = {"PATH_INFO": "/invalid/path"}
    res = pin_rest_cgi.application(env, start_response)
    assert len(res) == 1
    start_response.assert_called_once()
    assert start_response.call_args[0][0] == "400 Bad Request"


def test_pin_rest_cgi_invalid_method() -> None:
    """405 Method Not Allowed when HTTP method is GET."""
    start_response = MagicMock()
    env = {"PATH_INFO": "/pin/13", "REQUEST_METHOD": "GET"}
    res = pin_rest_cgi.application(env, start_response)
    assert len(res) == 1
    start_response.assert_called_once()
    assert start_response.call_args[0][0] == "405 Method Not Allowed"


def test_pin_rest_cgi_invalid_state() -> None:
    """400 Bad Request when state is invalid (neither ON nor OFF)."""
    start_response = MagicMock()
    body = b'{"state": "INVALID"}'
    env = {
        "PATH_INFO": "/pin/13",
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": BytesIO(body),
    }
    res = pin_rest_cgi.application(env, start_response)
    assert len(res) == 1
    start_response.assert_called_once()
    assert start_response.call_args[0][0] == "400 Bad Request"


def test_pin_rest_cgi_success_on(cfg: RuntimeConfig) -> None:
    """200 OK when setting pin state to ON."""
    start_response = MagicMock()
    body = b'{"state": "ON"}'
    env = {
        "PATH_INFO": "/pin/13",
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": BytesIO(body),
    }
    with patch("pin_rest_cgi.load_runtime_config", return_value=cfg):
        with patch("pin_rest_cgi.publish_sync") as mock_pub:
            res = pin_rest_cgi.application(env, start_response)
            assert len(res) == 1
            start_response.assert_called_once()
            assert start_response.call_args[0][0] == "200 OK"
            mock_pub.assert_called_once()


def test_pin_rest_cgi_success_off(cfg: RuntimeConfig) -> None:
    """200 OK when setting pin state to OFF."""
    start_response = MagicMock()
    body = b'{"state": "OFF"}'
    env = {
        "PATH_INFO": "/pin/5",
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": BytesIO(body),
    }
    with patch("pin_rest_cgi.load_runtime_config", return_value=cfg):
        with patch("pin_rest_cgi.publish_sync") as mock_pub:
            res = pin_rest_cgi.application(env, start_response)
            assert len(res) == 1
            start_response.assert_called_once()
            assert start_response.call_args[0][0] == "200 OK"
            mock_pub.assert_called_once()


def test_pin_rest_cgi_publish_sync_error(cfg: RuntimeConfig) -> None:
    """publish_sync raises Exception on local gRPC publication failure."""
    with patch("pin_rest_cgi.Channel") as mock_chan:
        mock_chan.side_effect = ConnectionError("socket missing")
        with pytest.raises(ConnectionError):
            pin_rest_cgi.publish_sync("br/digital/13", "1", cfg)


def test_pin_rest_cgi_run_cgi() -> None:
    """run_cgi executes CGIHandler.run()."""
    with patch("pin_rest_cgi.CGIHandler") as mock_handler:
        pin_rest_cgi.run_cgi()
        mock_handler.return_value.run.assert_called_once()


# ==============================================================================
# mcubridge_rotate_credentials.py (86% -> 100%)
# ==============================================================================


def test_update_uci_credentials_error() -> None:
    """update_uci_credentials exits with code 3 on UciException."""

    class MockUciException(Exception):
        pass

    mock_uci_cls = MagicMock()
    mock_uci_cls.return_value.set.side_effect = MockUciException("uci failure")

    with patch("mcubridge_rotate_credentials.uci.Uci", new=mock_uci_cls):
        with patch("mcubridge_rotate_credentials.uci.UciException", MockUciException):
            with pytest.raises(SystemExit) as exc_info:
                mcubridge_rotate_credentials.update_uci_credentials("sec", "pass")
    assert exc_info.value.code == 3


def test_restart_service_failure() -> None:
    """restart_service handles CalledProcessError gracefully."""
    import subprocess

    err = subprocess.CalledProcessError(1, ["/etc/init.d/mcubridge"], stderr=b"failed")
    with patch("subprocess.run", side_effect=err):
        mcubridge_rotate_credentials.restart_service()  # should not raise


def test_rotate_credentials_user_aborts() -> None:
    """main() exits with code 0 when user declines confirmation [N]."""
    with patch("sys.argv", ["rotate_credentials"]):
        with patch("sys.stdin.readline", return_value="n\n"):
            with pytest.raises(SystemExit) as exc_info:
                mcubridge_rotate_credentials.main()
    assert exc_info.value.code == 0


def test_rotate_credentials_force() -> None:
    """main() with --force completes rotation and restarts service."""
    with patch("sys.argv", ["rotate_credentials", "--force"]):
        with patch("mcubridge_rotate_credentials.update_uci_credentials") as mock_update:
            with patch("mcubridge_rotate_credentials.restart_service") as mock_restart:
                mcubridge_rotate_credentials.main()
                mock_update.assert_called_once()
                mock_restart.assert_called_once()


# ==============================================================================
# mcubridge_led_control.py (88% -> 100%)
# ==============================================================================


def test_led_control_invalid_state() -> None:
    """main() exits with code 2 when state is not on/off."""
    with patch("sys.argv", ["led_control", "blink"]):
        with pytest.raises(SystemExit) as exc_info:
            mcubridge_led_control.main()
    assert exc_info.value.code == 2


def test_led_control_success_on(cfg: RuntimeConfig) -> None:
    """main() calls do_publish for state='on'."""
    with patch("sys.argv", ["led_control", "on", "13"]):
        with patch("mcubridge_led_control.load_runtime_config", return_value=cfg):
            with patch("mcubridge_led_control.do_publish") as mock_pub:
                mcubridge_led_control.main()
                mock_pub.assert_called_once()


def test_led_control_do_publish_error() -> None:
    """do_publish exits with code 4 when gRPC raises."""
    with patch("mcubridge_led_control.Channel", side_effect=ConnectionError("fail")):
        with pytest.raises(SystemExit) as exc_info:
            mcubridge_led_control.do_publish("br/digital/13", "1")
    assert exc_info.value.code == 4


# ==============================================================================
# runtime.py — spool limit trimming, corrupt message dropping, console queues
# ==============================================================================


@pytest.mark.asyncio
async def test_runtime_spool_cloud_message_limit_trim(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_spool_cloud_message_locked appends message and reads pending count."""
    # [SIL-2] Trim-on-limit was removed during de-layering; _spool_cloud_message_locked
    # now directly appends and reads length via aiosqlite SqliteDeque.
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    mock_spool = AsyncMock()
    mock_spool.length = AsyncMock(return_value=3)
    mock_spool.append = AsyncMock()

    state.cloud_queue_limit = 5
    setattr(service, "_cloud_spool", mock_spool)

    msg = pb.CloudQueuedPublish(topic_name="br/test", payload=b"test")
    fn = getattr(service, "_spool_cloud_message_locked")
    res = await fn(msg)
    assert res is True
    mock_spool.append.assert_awaited_once()
    assert mock_spool.length.await_count == 2


@pytest.mark.asyncio
async def test_runtime_flush_cloud_spool_corrupt_entry(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_flush_cloud_spool_locked drops corrupt spool entries (lines 349-366)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    mock_spool = AsyncMock()
    # First peek returns invalid protobuf bytes, second peek raises IndexError
    mock_spool.peek = AsyncMock(side_effect=[b"\xff\xff\xff\xff", IndexError("empty")])
    mock_spool.length = AsyncMock(return_value=1)
    mock_spool.popleft = AsyncMock()

    setattr(service, "_cloud_spool", mock_spool)
    setattr(service, "_cloud_stream", MagicMock())

    fn = getattr(service, "_flush_cloud_spool_locked")
    await fn()
    assert state.cloud_spool_corrupt_dropped >= 1


@pytest.mark.asyncio
async def test_runtime_enqueue_cloud_console_queue(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """enqueue_cloud routes console messages to registered console queues (lines 255-257)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    q: asyncio.Queue[pb.CloudQueuedPublish] = asyncio.Queue()
    service.console_queues.append(q)

    msg = pb.CloudQueuedPublish(topic_name="br/console/output", payload=b"log")
    await service.enqueue_cloud(msg)
    assert q.qsize() == 1
