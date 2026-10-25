"""Deterministic verification suite for tenacity-based retry and waiting utilities (SIL-2)."""

from __future__ import annotations

import asyncio
import socket
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import tenacity
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.ubus import UbusService
from mcubridge.state.context import RuntimeState, create_runtime_state
from pytest_mock import MockerFixture

from tools.audit.sync_runtime_deps import fetch_url_with_retry
from tools.emulation.process_utils import wait_for_path_ready, wait_for_tcp_ready


class TypedRuntimeFacade:
    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self.serial = MagicMock()
        self.handle_request = AsyncMock()
        self.process_run_async = AsyncMock(return_value=pb.ProcessRunAsyncResponse(pid=123))
        self.process_kill = AsyncMock(return_value=pb.GenericResponse(status="ok"))
        self.reset_link = AsyncMock(return_value=True)
        self.file_write = AsyncMock(return_value=True)
        self.file_read = AsyncMock(return_value=b"")
        self.file_remove = AsyncMock(return_value=True)
        self.datastore_put = AsyncMock(return_value=True)
        self.datastore_get = AsyncMock(return_value=b"")
        self.mailbox_push = AsyncMock(return_value=True)
        self.mailbox_read = AsyncMock(return_value=b"")
        self.flush_console_queue = AsyncMock()
        self.console_queues: list[Any] = []
        self.ipc_requests: dict[bytes, asyncio.Queue[pb.CloudQueuedPublish]] = {}
        self.clock_sync = MagicMock()
        self.gpio = MagicMock()
        self.process_poll = AsyncMock(
            return_value=pb.ProcessPollResponse(
                status=0,
                exit_code=0,
                finished=True,
                stdout_data=b"test",
                stderr_data=b"",
                stdout_truncated=False,
                stderr_truncated=False,
            )
        )


def _make_runtime() -> TypedRuntimeFacade:
    cfg = RuntimeConfig()
    st = create_runtime_state(cfg)
    return TypedRuntimeFacade(cfg, st)


def test_wait_for_path_ready_immediate(tmp_path: Path) -> None:
    test_file = tmp_path / "ready.txt"
    test_file.write_text("ok", encoding="utf-8")
    assert wait_for_path_ready(test_file, timeout=1.0, interval=0.01) is True


def test_wait_for_path_ready_delayed(tmp_path: Path) -> None:
    test_file = tmp_path / "delayed.txt"

    def _create_later() -> None:
        import time

        time.sleep(0.05)
        test_file.write_text("ok", encoding="utf-8")

    t = threading.Thread(target=_create_later)
    t.start()
    try:
        assert wait_for_path_ready(test_file, timeout=2.0, interval=0.01) is True
    finally:
        t.join()


def test_wait_for_path_ready_timeout(tmp_path: Path) -> None:
    test_file = tmp_path / "never.txt"
    assert wait_for_path_ready(test_file, timeout=0.1, interval=0.02) is False


def test_wait_for_tcp_ready_success() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _accept() -> None:
        try:
            conn, _ = srv.accept()
            conn.close()
        except OSError as exc:
            import sys

            sys.stderr.write(f"[DEBUG] Mock server socket closed: {exc}\n")

    t = threading.Thread(target=_accept)
    t.start()
    try:
        assert wait_for_tcp_ready("127.0.0.1", port, timeout=2.0, interval=0.02) is True
    finally:
        srv.close()
        t.join()


def test_wait_for_tcp_ready_timeout() -> None:
    assert wait_for_tcp_ready("127.0.0.1", 59999, timeout=0.15, interval=0.03) is False


def test_ubus_service_start_retries_and_succeeds(mocker: MockerFixture) -> None:

    mock_ubus: Any = MagicMock()
    mock_conn: Any = MagicMock()
    attempts = 0

    def _mock_connect() -> Any:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError(f"ubusd busy, attempt={attempts}")
        return mock_conn

    mock_ubus.connect.side_effect = _mock_connect
    mock_ubus.INT32 = 1
    mock_ubus.STRING = 2

    service = UbusService(_make_runtime(), ubus_module=mock_ubus)

    res = service.start(max_attempts=4, retry_wait=tenacity.wait_none())
    assert res is True
    assert service.is_active is True
    assert service.connection == mock_conn
    assert attempts == 3


def test_ubus_service_start_retry_exhaustion() -> None:
    mock_ubus: Any = MagicMock()
    mock_ubus.connect.side_effect = OSError("Connection refused")

    service = UbusService(_make_runtime(), ubus_module=mock_ubus)

    res = service.start(max_attempts=3, retry_wait=tenacity.wait_none())
    assert res is False
    assert service.is_active is False
    assert mock_ubus.connect.call_count == 3


def test_fetch_url_with_retry_succeeds(mocker: MockerFixture) -> None:
    attempts = 0

    def _mock_get(url: str, headers: Any = None, timeout: float = 10.0) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("Temporary DNS glitch")
        resp = MagicMock(spec=httpx.Response)
        resp.content = b"OK"
        return resp

    mocker.patch.object(httpx, "get", side_effect=_mock_get)
    resp = fetch_url_with_retry("http://test.local/pkg", attempts=3)
    assert resp.content == b"OK"
    assert attempts == 2


def test_fetch_url_with_retry_exhaustion(mocker: MockerFixture) -> None:
    mocker.patch.object(
        httpx,
        "get",
        side_effect=httpx.ConnectError("Fatal host unreachable"),
    )
    with pytest.raises(httpx.HTTPError):
        fetch_url_with_retry("http://test.local/pkg", attempts=2)
