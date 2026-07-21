import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_spool_limit_exceptions():
    cfg = load_runtime_config()
    cfg.cloud_queue_limit = 1
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()

    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)

    # 1. IndexError on popleft during limit trim
    mock_spool = AsyncMock()
    mock_spool.length.side_effect = [2, 0]
    mock_spool.popleft.side_effect = IndexError("Empty deque")
    srv._cloud_spool = mock_spool

    msg = pb.CloudQueuedPublish(topic_name="test/topic", payload=b"data")
    res = await srv._spool_cloud_message_locked(msg)
    assert res is True

    # 2. OSError on popleft during limit trim
    mock_spool = AsyncMock()
    mock_spool.length.side_effect = [2, 0]
    mock_spool.popleft.side_effect = OSError("DB lock")
    srv._cloud_spool = mock_spool

    res = await srv._spool_cloud_message_locked(msg)
    assert res is True


@pytest.mark.asyncio
async def test_flush_spool_exceptions():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()

    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)
    srv._cloud_stream = AsyncMock()

    # 1. length() OSError
    mock_spool = AsyncMock()
    mock_spool.length.side_effect = OSError("DB error")
    srv._cloud_spool = mock_spool

    await srv._flush_cloud_spool_locked()
    assert service.state.cloud_spool_degraded is True

    # 2. popleft IndexError after successful publish
    mock_spool = AsyncMock()
    msg = pb.CloudQueuedPublish(topic_name="test/topic", payload=b"data")
    mock_spool.length.side_effect = [1, 0]
    mock_spool.peek.return_value = msg.SerializeToString()
    mock_spool.popleft.side_effect = IndexError("Empty pop")
    srv._cloud_spool = mock_spool

    with patch.object(service, "_publish_cloud_message", new_callable=AsyncMock, return_value=True):
        await srv._flush_cloud_spool_locked()

    # 3. popleft OSError after successful publish
    mock_spool = AsyncMock()
    mock_spool.length.side_effect = [1, 0]
    mock_spool.peek.return_value = msg.SerializeToString()
    mock_spool.popleft.side_effect = OSError("DB error")
    srv._cloud_spool = mock_spool

    with patch.object(service, "_publish_cloud_message", new_callable=AsyncMock, return_value=True):
        await srv._flush_cloud_spool_locked()
    assert service.state.cloud_spool_degraded is True


@pytest.mark.asyncio
async def test_runtime_service_extra_paths():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = MagicMock()
    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)
    service.serial = None  # type: ignore[assignment]

    # _request_mcu_version when serial is None
    res = await srv._request_mcu_version()
    assert res is False

    # _flush_console_queue when queue empty
    await srv._flush_console_queue()

    # _poll_process when process not found
    resp = await srv._poll_process(99999)
    assert resp.exit_code == 1
    assert resp.finished is True


@pytest.mark.asyncio
async def test_local_bridge_service_publish_correlation_timeout():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = MagicMock()
    service = BridgeService(cfg, state, mock_serial)

    from mcubridge.services.runtime import LocalBridgeService

    lbs = LocalBridgeService(service)

    # Test Publish with correlation data and TimeoutError
    mock_stream = AsyncMock()
    msg = pb.CloudQueuedPublish(topic_name="test/topic", payload=b"data", correlation_data=b"123456789012")
    mock_stream.recv_message.return_value = msg

    with (
        patch.object(service, "handle_request", new_callable=AsyncMock),
        patch("asyncio.timeout", side_effect=TimeoutError),
    ):
        await lbs.Publish(mock_stream)
        mock_stream.send_message.assert_called_once()
        assert mock_stream.send_message.call_args[0][0].topic_name == ""


@pytest.mark.asyncio
async def test_local_bridge_service_publish_oserror():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = MagicMock()
    service = BridgeService(cfg, state, mock_serial)

    from mcubridge.services.runtime import LocalBridgeService

    lbs = LocalBridgeService(service)

    mock_stream = AsyncMock()
    msg = pb.CloudQueuedPublish(topic_name="test/topic", payload=b"data")
    mock_stream.recv_message.return_value = msg
    mock_stream.send_message.side_effect = OSError("Pipe broken")

    with patch.object(service, "handle_request", new_callable=AsyncMock):
        await lbs.Publish(mock_stream)


@pytest.mark.asyncio
async def test_run_ipc_chmod_oserror():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = MagicMock()
    service = BridgeService(cfg, state, mock_serial)

    with patch("os.makedirs"), patch("os.chmod", side_effect=OSError("ReadOnly FS")):
        with patch("mcubridge.services.runtime.Server") as mock_server_cls:
            mock_server_inst = MagicMock()
            mock_server_cls.return_value = mock_server_inst
            mock_server_inst.start = AsyncMock()
            mock_server_inst.wait_closed = AsyncMock()
            mock_server_inst.close = MagicMock()
            await service.run_ipc_server()
            mock_server_inst.close.assert_called_once()


@pytest.mark.asyncio
async def test_run_cloud_coverage():
    import tenacity

    cfg = load_runtime_config()
    cfg.cloud_enabled = False
    state = create_runtime_state(cfg)
    mock_serial = MagicMock()
    service = BridgeService(cfg, state, mock_serial)

    # 1. Cloud disabled
    await service.run_cloud()

    # 2. Cloud enabled, successful connect
    cfg.cloud_enabled = True
    with patch.object(service, "connect_cloud_session", new_callable=AsyncMock):
        await service.run_cloud()

    # 3. Cloud enabled, CancelledError
    with patch.object(service, "connect_cloud_session", new_callable=AsyncMock, side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await service.run_cloud()

    # 4. Cloud enabled, RetryError / OSError
    mock_retryer = AsyncMock(side_effect=tenacity.RetryError(last_attempt=MagicMock()))
    with patch("tenacity.AsyncRetrying", return_value=mock_retryer):
        with pytest.raises(tenacity.RetryError):
            await service.run_cloud()


@pytest.mark.asyncio
async def test_handshake_fetch_capabilities_with_delay():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = MagicMock()
    service = BridgeService(cfg, state, mock_serial)
    hs = cast(Any, service.handshake)

    with (
        patch("asyncio.sleep", new_callable=AsyncMock),
        patch.object(hs, "_fetch_capabilities", new_callable=AsyncMock) as mock_fetch,
    ):
        await hs._fetch_capabilities_with_delay()
        mock_fetch.assert_called_once()


@pytest.mark.asyncio
async def test_serial_send_frame_exceptions():
    from mcubridge.protocol.protocol import Command
    from mcubridge.transport.serial import SerialTransport

    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = MagicMock()
    service = BridgeService(cfg, state, mock_serial)
    st = SerialTransport(cfg, state, service)
    st_any = cast(Any, st)
    st_any._port = MagicMock()

    # 1. send_raw returns False -> FatalSerialError
    with patch.object(st, "send_raw", new_callable=AsyncMock, return_value=False):
        res = await st.send(Command.CMD_GET_CAPABILITIES.value, b"")
        assert res is False

    # 2. pending completion timeout -> RetryableSerialError
    st_any._retry_attempts = 1
    with (
        patch.object(st, "send_raw", new_callable=AsyncMock, return_value=True),
        patch("asyncio.timeout", side_effect=TimeoutError),
    ):
        res = await st.send(Command.CMD_GET_CAPABILITIES.value, b"")
        assert res is False


@pytest.mark.asyncio
async def test_connect_cloud_session_coverage():
    cfg = load_runtime_config()
    cfg.cloud_http3_enabled = True
    state = create_runtime_state(cfg)
    mock_serial = MagicMock()
    service = BridgeService(cfg, state, mock_serial)

    class MockAsyncStream:
        def __init__(self, items: list[Any]):
            self.items = items

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
            pass

        def __aiter__(self) -> Any:
            return self._gen()

        async def _gen(self) -> Any:
            for item in self.items:
                yield item

        async def send_message(self, msg: Any) -> None:
            pass

    mock_stub = MagicMock()
    pong_env = pb.CloudEnvelope(pong=pb.KeepalivePong())
    cmd_env = pb.CloudEnvelope(
        sequence_id=1, command_request=pb.CommandRequest(command_path="mcu/pin/13", payload=b"1")
    )
    stream_inst = MockAsyncStream([pong_env, cmd_env])
    mock_stub.Session.open.return_value = stream_inst

    with (
        patch("mcubridge.services.runtime.Channel") as mock_chan,
        patch("mcubridge.services.runtime.CloudBridgeStub", return_value=mock_stub),
        patch.object(service, "flush_cloud_spool", new_callable=AsyncMock),
    ):
        mock_chan_inst = MagicMock()
        mock_chan.return_value = mock_chan_inst
        await service.connect_cloud_session(None)


@pytest.mark.asyncio
async def test_send_cloud_event_coverage():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = MagicMock()
    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)
    srv._cloud_stream = AsyncMock()

    await srv._send_cloud_event("test_event", "info", "test description")
    srv._cloud_stream.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_cloud_incoming_worker_error():
    from mcubridge.services.runtime import BridgeRequest

    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = MagicMock()
    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)

    req = BridgeRequest(topic="mcu/pin/13", payload=b"1")
    await srv._cloud_incoming_queue.put(req)

    with patch.object(service, "handle_request", new_callable=AsyncMock, side_effect=ValueError("Invalid request")):
        task = asyncio.create_task(srv._cloud_incoming_worker())
        await srv._cloud_incoming_queue.join()
        task.cancel()
        await task


@pytest.mark.asyncio
async def test_lifecycle_and_serial_events():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    service = BridgeService(cfg, state, mock_serial)

    # on_serial_connected
    with patch.object(service.handshake, "synchronize", new_callable=AsyncMock):
        state.mark_synchronized()
        with (
            patch.object(service, "_request_mcu_version", new_callable=AsyncMock),
            patch.object(service, "_flush_console_queue", new_callable=AsyncMock),
        ):
            await service.on_serial_connected()

    # on_serial_disconnected
    await service.on_serial_disconnected()

    # cleanup and __del__
    with patch("os.path.exists", return_value=True), patch("os.unlink", side_effect=OSError("Remove error")):
        service.cleanup()
        service.__del__()


@pytest.mark.asyncio
async def test_publish_cloud_message_exceptions():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = MagicMock()
    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)
    mock_stream = AsyncMock()
    mock_stream.send_message.side_effect = OSError("Socket error")
    srv._cloud_stream = mock_stream

    msg = pb.CloudQueuedPublish(topic_name="test/topic", payload=b"123")
    res = await srv._publish_cloud_message(msg)
    assert res is False


@pytest.mark.asyncio
async def test_reject_cloud_and_disk_free_error():
    from pathlib import Path
    from mcubridge.protocol.topics import Topic

    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = MagicMock()
    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)

    # test _reject_cloud
    with patch.object(service, "enqueue_cloud", new_callable=AsyncMock) as mock_enqueue:
        await srv._reject_cloud(None, Topic.DIGITAL, "write")
        mock_enqueue.assert_called_once()

    # test _write_with_quota disk full error
    with patch("shutil.disk_usage") as mock_usage:
        mock_usage.return_value = MagicMock(used=1000, free=10)
        res = await srv._write_with_quota(Path("/tmp/test.txt"), b"x" * 100)
        assert res is False


@pytest.mark.asyncio
async def test_mcu_process_poll_and_fail():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)

    # Process finalize
    srv._finalize_process(999)

    # Process poll
    with patch.object(srv, "_poll_process", new_callable=AsyncMock) as mock_poll:
        mock_poll.return_value = pb.ProcessPollResponse(exit_code=0, finished=True)
        res = await srv._on_mcu_process_poll(1, pb.ProcessPoll(pid=1))
        assert res is True


@pytest.mark.asyncio
async def test_handshake_synchronize_failure_paths():
    import tenacity

    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = MagicMock()
    service = BridgeService(cfg, state, mock_serial)
    hs = cast(Any, service.handshake)

    # 1. RetryError on synchronize
    mock_retryer = AsyncMock(side_effect=tenacity.RetryError(last_attempt=MagicMock()))
    with patch("tenacity.AsyncRetrying", return_value=mock_retryer):
        res = await hs.synchronize()
        assert res is False

    # 2. link_reset_send_failed
    with patch.object(hs, "_send_frame", new_callable=AsyncMock, return_value=False):
        res = await hs._synchronize_attempt()
        assert res is False

    # 3. link_sync_send_failed
    with (
        patch.object(hs, "_send_frame", new_callable=AsyncMock, side_effect=[True, False]),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        res = await hs._synchronize_attempt()
        assert res is False

    # 4. confirm timeout
    with (
        patch.object(hs, "_send_frame", new_callable=AsyncMock, return_value=True),
        patch("asyncio.sleep", new_callable=AsyncMock),
        patch.object(hs, "_wait_for_link_sync_confirmation", new_callable=AsyncMock, return_value=False),
    ):
        res = await hs._synchronize_attempt()
        assert res is False


@pytest.mark.asyncio
async def test_handle_file_and_mailbox_extra_paths():
    from mcubridge.services.runtime import BridgeRequest
    from mcubridge.protocol.structures import TopicRoute

    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    mock_serial.send.return_value = True
    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)

    # 1. File write on MCU_FS_PREFIX
    req = BridgeRequest(topic="file/write/mcu/sd/data.txt", payload=b"hello")
    route = TopicRoute(raw="", prefix="", topic="file", segments=("write", "mcu", "sd", "data.txt"))
    with patch.object(service, "enqueue_cloud", new_callable=AsyncMock):
        await srv._handle_file(route, req)

    # 2. File remove on MCU_FS_PREFIX
    route = TopicRoute(raw="", prefix="", topic="file", segments=("remove", "mcu", "sd", "data.txt"))
    await srv._handle_file(route, req)

    # 3. File write on local host path
    route = TopicRoute(raw="", prefix="", topic="file", segments=("write", "tmp", "test.txt"))
    with (
        patch.object(service, "enqueue_cloud", new_callable=AsyncMock),
        patch.object(srv, "_write_with_quota", new_callable=AsyncMock, return_value=True),
    ):
        await srv._handle_file(route, req)

    # 4. File read on local host path
    route = TopicRoute(raw="", prefix="", topic="file", segments=("read", "tmp", "test.txt"))
    with (
        patch.object(service, "enqueue_cloud", new_callable=AsyncMock),
        patch("pathlib.Path.is_file", return_value=True),
        patch("pathlib.Path.read_bytes", return_value=b"data"),
    ):
        await srv._handle_file(route, req)

    # 5. Mailbox write and read
    req_mb = BridgeRequest(topic="mailbox/write", payload=b"msg")
    route_mb_write = TopicRoute(raw="", prefix="", topic="mailbox", segments=("write",))
    await srv._handle_mailbox(route_mb_write, req_mb)

    route_mb_read = TopicRoute(raw="", prefix="", topic="mailbox", segments=("read",))
    with patch.object(service, "enqueue_cloud", new_callable=AsyncMock):
        await srv._handle_mailbox(route_mb_read, req_mb)


@pytest.mark.asyncio
async def test_handle_shell_spi_pin_extra_paths():
    from mcubridge.services.runtime import BridgeRequest
    from mcubridge.protocol.structures import TopicRoute

    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    mock_serial.send.return_value = True
    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)

    # 1. Shell RUN_ASYNC with invalid payload, POLL, KILL
    req_shell = BridgeRequest(topic="shell/run_async", payload=b"\xff\xff invalid")
    route_shell_async = TopicRoute(raw="", prefix="", topic="shell", segments=("run_async",))
    with patch.object(service, "enqueue_cloud", new_callable=AsyncMock):
        await srv._handle_shell(route_shell_async, req_shell)

    route_shell_poll = TopicRoute(raw="", prefix="", topic="shell", segments=("poll", "123"))
    with (
        patch.object(srv, "_poll_process", new_callable=AsyncMock) as m_poll,
        patch.object(service, "enqueue_cloud", new_callable=AsyncMock),
    ):
        m_poll.return_value = pb.ProcessPollResponse(exit_code=0, finished=True)
        await srv._handle_shell(route_shell_poll, req_shell)

    route_shell_kill = TopicRoute(raw="", prefix="", topic="shell", segments=("kill", "123"))
    with patch.object(srv, "_stop_process", new_callable=AsyncMock):
        await srv._handle_shell(route_shell_kill, req_shell)

    # 2. SPI BEGIN, END, CONFIG error, TRANSFER
    route_spi_begin = TopicRoute(raw="", prefix="", topic="spi", segments=("begin",))
    await srv._handle_spi(route_spi_begin, req_shell)

    route_spi_end = TopicRoute(raw="", prefix="", topic="spi", segments=("end",))
    await srv._handle_spi(route_spi_end, req_shell)

    route_spi_config = TopicRoute(raw="", prefix="", topic="spi", segments=("config",))
    await srv._handle_spi(route_spi_config, req_shell)

    route_spi_tr = TopicRoute(raw="", prefix="", topic="spi", segments=("transfer",))
    mock_serial.send.return_value = pb.SpiTransferResponse(data=b"resp").SerializeToString()
    with patch.object(service, "enqueue_cloud", new_callable=AsyncMock):
        await srv._handle_spi(route_spi_tr, req_shell)

    # 3. Pin MODE, READ with limit reached
    from mcubridge.protocol.topics import Topic

    req_pin = BridgeRequest(topic="digital/13/mode", payload=b"1")
    route_pin_mode = TopicRoute(raw="", prefix="", topic=Topic.DIGITAL, segments=("13", "mode"))
    await srv._handle_pin(route_pin_mode, req_pin)

    state.pending_pin_request_limit = 0
    route_pin_read = TopicRoute(raw="", prefix="", topic=Topic.DIGITAL, segments=("13", "read"))
    with patch.object(service, "enqueue_cloud", new_callable=AsyncMock):
        await srv._handle_pin(route_pin_read, req_pin)


@pytest.mark.asyncio
async def test_serial_transport_exhaustive_gap_closure():
    from mcubridge.protocol import protocol
    from mcubridge.transport.serial import SerialTransport

    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    st = SerialTransport(cfg, state, mock_serial)
    st_any = cast(Any, st)

    # 1. _toggle_dtr with Exception
    mock_serial.set_modem_pins.side_effect = OSError("DTR Fail")
    await st_any._toggle_dtr()

    # 2. stop method
    await st.stop()
    assert st_any._stop_event.is_set()

    # 3. send_raw with TX flow control timeout and synchronized nonce
    state.serial_tx_allowed.clear()
    state.state = "synchronized"
    with patch("asyncio.timeout", side_effect=TimeoutError):
        await st.send_raw(protocol.Command.CMD_DIGITAL_WRITE.value, b"\x01")

    # 4. _negotiate_baudrate send_raw failure & timeout
    with patch.object(st, "send_raw", new_callable=AsyncMock, return_value=False):
        res = await st_any._negotiate_baudrate(115200)
        assert res is False

    with (
        patch.object(st, "send_raw", new_callable=AsyncMock, return_value=True),
        patch("asyncio.wait_for", side_effect=TimeoutError),
    ):
        res = await st_any._negotiate_baudrate(115200)
        assert res is False

    # 5. _process_packet with anti-replay validation failure
    state.state = "synchronized"
    with (
        patch("mcubridge.transport.serial.cobsr.decode", return_value=b"frame"),
        patch("mcubridge.transport.serial.parse_frame") as m_pf,
        patch("mcubridge.transport.serial.validate_nonce_counter", return_value=(False, 0)),
    ):
        m_env = MagicMock()
        m_env.command_id = protocol.Command.CMD_DIGITAL_WRITE.value
        m_env.nonce = b"\x00" * 12
        m_pf.return_value = MagicMock(envelope=m_env, payload=b"")
        await st_any._process_packet(b"encoded")

    # 6. send method timeout / Fatal error
    st.serial = mock_serial
    with patch.object(st, "send_raw", new_callable=AsyncMock, return_value=False):
        res = await st.send(protocol.Command.CMD_DIGITAL_WRITE.value, b"")
        assert res is False
