"""Test-only helpers for constructing arbitrary Pigweed packets in protocol tests."""

from __future__ import annotations

from typing import Any, cast

from google.protobuf.message import Message
from pw_rpc.internal.packet_pb2 import PacketType, RpcPacket

from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Status


def encode_rpc_packet(
    target: int | str | tuple[int, int],
    payload: bytes | Message = b"",
    *,
    call_id: int = 1,
    channel_id: int = protocol.HDLC_ADDRESS_RPC,
    packet_type: int = int(PacketType.REQUEST),
    status: Status = Status.OK,
) -> bytes:
    """Build arbitrary RpcPacket bytes for tests and fuzzing only."""
    spec = protocol.get_operation(target)
    actual_type = packet_type
    if actual_type == int(PacketType.REQUEST) and spec is not None:
        if (
            spec.response_type is not None
            and spec.response_type is not spec.request_type
            and isinstance(payload, Message)
            and isinstance(payload, spec.response_type)
        ):
            actual_type = int(PacketType.SERVER_STREAM if spec.is_server_streaming else PacketType.RESPONSE)
        elif payload == b"" and spec.is_server_streaming:
            actual_type = int(PacketType.RESPONSE)

    if spec is not None and spec.service_id is not None and spec.method_id is not None:
        service_id, method_id = cast(int, spec.service_id), cast(int, spec.method_id)
    elif isinstance(target, tuple):
        service_id, method_id = target
    elif isinstance(target, int):
        service_id = method_id = target
    else:
        service_id = method_id = 0

    raw_payload = payload.SerializeToString() if isinstance(payload, Message) else payload
    return RpcPacket(
        type=cast(Any, actual_type),
        channel_id=channel_id,
        service_id=cast(int, service_id),
        method_id=cast(int, method_id),
        call_id=call_id,
        status=status.value,
        payload=raw_payload,
    ).SerializeToString()
