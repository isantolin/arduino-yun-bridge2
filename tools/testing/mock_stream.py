"""Declarative Mock Stream Harness for grpclib gRPC streaming and unary RPCs (SIL-2)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any, cast
from unittest.mock import AsyncMock

from grpclib.protocol import Peer
from grpclib.server import Stream


class MockPeer(Peer):
    """Mock grpclib Peer object representing network identity and TLS credentials."""

    def __init__(
        self,
        addr: tuple[str, int] = ("127.0.0.1", 54321),
        cert: dict[str, Any] | None = None,
        common_name: str | None = None,
    ) -> None:
        self._addr = addr
        if cert is not None:
            self._cert: dict[str, Any] | None = cert
        elif common_name is not None:
            self._cert = {"subject": [[("commonName", common_name)]]}
        else:
            self._cert = None

    def addr(self) -> tuple[str, int]:
        return self._addr

    def cert(self) -> dict[str, Any] | None:
        return self._cert


class _DefaultPeer:
    pass


_DEFAULT_PEER = _DefaultPeer()


class MockGrpcStream[RecvMsg = Any, SendMsg = Any]:
    """Declarative Mock Stream Harness for grpclib bidirectional streams and unary RPCs."""

    def __init__(
        self,
        request: RecvMsg | None = None,
        inbound: Sequence[RecvMsg] | None = None,
        metadata: Mapping[str, Any] | None = None,
        peer_addr: tuple[str, int] = ("127.0.0.1", 54321),
        peer_cert: dict[str, Any] | None = None,
        client_cn: str | None = None,
        peer: MockPeer | None | _DefaultPeer = _DEFAULT_PEER,
        send_exception: BaseException | None = None,
        send_side_effect: Callable[[SendMsg], Any] | None = None,
        recv_exception: BaseException | None = None,
        aiter_exception: BaseException | None = None,
    ) -> None:
        self.metadata: Mapping[str, Any] | None = metadata
        if isinstance(peer, _DefaultPeer):
            self.peer: MockPeer | None = MockPeer(addr=peer_addr, cert=peer_cert, common_name=client_cn)
        else:
            self.peer = peer
        self._inbound: list[RecvMsg] = (
            list(inbound) if inbound is not None else ([request] if request is not None else [])
        )
        self._aiter_exception = aiter_exception
        self.sent_messages: list[SendMsg] = []

        async def _mock_send(msg: SendMsg) -> None:
            if send_exception is not None:
                raise send_exception
            if send_side_effect is not None:
                res = send_side_effect(msg)
                if asyncio.iscoroutine(res):
                    await res
            self.sent_messages.append(msg)

        self.send_message: AsyncMock = AsyncMock(side_effect=_mock_send)

        if recv_exception is not None:
            self.recv_message: AsyncMock = AsyncMock(side_effect=recv_exception)
        elif inbound is not None:
            self.recv_message = AsyncMock(side_effect=list(inbound))
        else:
            self.recv_message = AsyncMock(return_value=request)

    def __aiter__(self) -> AsyncIterator[RecvMsg]:
        async def _iterator() -> AsyncIterator[RecvMsg]:
            for msg in self._inbound:
                yield msg
            if self._aiter_exception is not None:
                raise self._aiter_exception

        return _iterator()

    async def __aenter__(self) -> MockGrpcStream[RecvMsg, SendMsg]:
        return self

    async def __aexit__(self, *args: object) -> None:
        _ = args

    @property
    def sent_message(self) -> SendMsg:
        """Convenience property returning the first sent message."""
        if not self.sent_messages:
            raise AssertionError("No message was sent on MockGrpcStream")
        return self.sent_messages[0]

    @property
    def last_sent_message(self) -> SendMsg:
        """Convenience property returning the most recently sent message."""
        if not self.sent_messages:
            raise AssertionError("No message was sent on MockGrpcStream")
        return self.sent_messages[-1]

    def reset(self) -> None:
        """Reset sent messages and mock invocation history."""
        self.sent_messages.clear()
        self.send_message.reset_mock()
        self.recv_message.reset_mock()

    def as_stream(self) -> Stream[RecvMsg, SendMsg]:
        """Cast this harness to grpclib Stream for passing to strictly typed RPC handlers."""
        return cast(Stream[RecvMsg, SendMsg], self)
