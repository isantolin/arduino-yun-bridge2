"""Deterministic tests for the Declarative Mock Stream Harness (SIL-2)."""

from __future__ import annotations

import asyncio
import pytest
from tools.testing.mock_stream import MockGrpcStream, MockPeer


def test_mock_peer_attributes() -> None:
    peer_default = MockPeer()
    assert peer_default.addr() == ("127.0.0.1", 54321)
    assert peer_default.cert() is None

    peer_cn = MockPeer(addr=("10.0.0.1", 12345), common_name="device-node-1")
    assert peer_cn.addr() == ("10.0.0.1", 12345)
    assert peer_cn.cert() == {"subject": [[("commonName", "device-node-1")]]}

    custom_cert = {"subject": [[("O", "Org")]]}
    peer_custom = MockPeer(cert=custom_cert)
    assert peer_custom.cert() == custom_cert

    stream_none_peer: MockGrpcStream[str, str] = MockGrpcStream(peer=None)
    assert stream_none_peer.peer is None


@pytest.mark.asyncio
async def test_mock_grpc_stream_unary_request() -> None:
    stream: MockGrpcStream[str, str] = MockGrpcStream(request="hello", metadata={"x-dev": "1"})
    assert stream.metadata == {"x-dev": "1"}
    assert await stream.recv_message() == "hello"

    await stream.send_message("world")
    assert stream.sent_messages == ["world"]
    assert stream.sent_message == "world"
    assert stream.last_sent_message == "world"
    assert stream.send_message.called


@pytest.mark.asyncio
async def test_mock_grpc_stream_streaming_inbound() -> None:
    stream: MockGrpcStream[int, int] = MockGrpcStream(inbound=[1, 2, 3])

    received: list[int] = [item async for item in stream]
    assert received == [1, 2, 3]

    # Multiple sends
    await stream.send_message(10)
    await stream.send_message(20)
    assert stream.sent_messages == [10, 20]
    assert stream.sent_message == 10
    assert stream.last_sent_message == 20

    stream.reset()
    assert stream.sent_messages == []
    assert not stream.send_message.called


@pytest.mark.asyncio
async def test_mock_grpc_stream_exceptions() -> None:
    # 1. aiter_exception
    stream_iter: MockGrpcStream[str, str] = MockGrpcStream(
        inbound=["first"], aiter_exception=ConnectionResetError("closed")
    )
    with pytest.raises(ConnectionResetError, match="closed"):
        async for _ in stream_iter:
            pass

    # 2. send_exception
    stream_send: MockGrpcStream[str, str] = MockGrpcStream(send_exception=OSError("network down"))
    with pytest.raises(OSError, match="network down"):
        await stream_send.send_message("fail")

    # 3. recv_exception
    stream_recv: MockGrpcStream[str, str] = MockGrpcStream(recv_exception=RuntimeError("recv error"))
    with pytest.raises(RuntimeError, match="recv error"):
        await stream_recv.recv_message()

    # 4. sent_message on empty list
    stream_empty: MockGrpcStream[str, str] = MockGrpcStream()
    with pytest.raises(AssertionError, match="No message was sent"):
        _ = stream_empty.sent_message
    with pytest.raises(AssertionError, match="No message was sent"):
        _ = stream_empty.last_sent_message


@pytest.mark.asyncio
async def test_mock_grpc_stream_context_and_side_effects() -> None:
    async def _async_effect(msg: str) -> None:
        await asyncio.sleep(0.001)

    stream: MockGrpcStream[str, str] = MockGrpcStream(send_side_effect=_async_effect)
    async with stream as active_stream:
        assert active_stream is stream
        await active_stream.send_message("async_done")
    assert stream.sent_messages == ["async_done"]
