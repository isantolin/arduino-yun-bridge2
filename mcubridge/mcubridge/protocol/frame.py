"""RPC frame building and parsing for Arduino-Linux serial communication.

This module implements the binary frame format used over the serial link
between the Linux daemon and the Arduino MCU.

[SIL-2 COMPLIANCE]
- Zero manual orchestration logic.
- Direct Protobuf library usage (no wrappers).
- Explicit CRC32 validation.
"""

from __future__ import annotations

from binascii import crc32
from typing import Final, NamedTuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from google.protobuf.message import DecodeError, Message as ProtobufMessage

from mcubridge.protocol import mcubridge_pb2 as pb

from . import protocol, is_system_command

_PAYLOAD_FIELD_MAP: dict[str, str] = {
    f.message_type.name: f.name
    for f in pb.RpcEnvelope.DESCRIPTOR.oneofs_by_name["payload_type"].fields
    if f.message_type is not None
}

_NONCE_SIZE: Final = protocol.AEAD_NONCE_SIZE
_TAG_SIZE: Final = protocol.AEAD_TAG_SIZE
_CRC_SIZE: Final = protocol.CRC_SIZE


class DecodedFrame(NamedTuple):
    envelope: pb.RpcEnvelope
    payload: bytes | ProtobufMessage


def build_frame(
    command_id: int,
    sequence_id: int,
    payload: bytes | ProtobufMessage = b"",
    nonce: bytes | None = None,
    tag: bytes | None = None,
    session_key: bytes | None = None,
) -> bytes:
    """Builds a binary frame using a Protobuf envelope directly. [SIL-2]"""
    if not (0 <= command_id <= protocol.UINT16_MAX):
        raise ValueError(f"Invalid command ID: {command_id}")

    is_excluded = is_system_command(command_id)

    # Initialize RpcEnvelope directly
    envelope = pb.RpcEnvelope(
        version=protocol.PROTOCOL_VERSION,
        command_id=command_id,
        sequence_id=sequence_id,
        nonce=nonce or (b"\x00" * _NONCE_SIZE),
    )

    # AEAD Encryption (if session key provided)
    do_encrypt = bool(session_key and not is_excluded)

    if do_encrypt:
        assert session_key is not None
        payload_bytes = payload.SerializeToString() if isinstance(payload, ProtobufMessage) else payload
        if len(payload_bytes) > protocol.MAX_PAYLOAD_SIZE:
            raise ValueError(f"Payload size {len(payload_bytes)} exceeds maximum {protocol.MAX_PAYLOAD_SIZE}")

        # Optimization: Use Protobuf envelope itself as AAD by only including header fields.
        aad = pb.RpcEnvelope(
            version=envelope.version, command_id=envelope.command_id, sequence_id=envelope.sequence_id
        ).SerializeToString()

        envelope.encrypted_payload_with_tag = ChaCha20Poly1305(session_key).encrypt(envelope.nonce, payload_bytes, aad)
    else:
        # Unencrypted! [SIL-2] Holistic payload extraction natively handled by Protobuf.
        if isinstance(payload, ProtobufMessage):
            # [SIL-2] Use descriptor-based field mapping to eliminate manual string logic.
            field_name = _PAYLOAD_FIELD_MAP.get(payload.DESCRIPTOR.name)
            if field_name:
                getattr(envelope, field_name).CopyFrom(payload)
        else:
            if len(payload) > protocol.MAX_PAYLOAD_SIZE:
                raise ValueError(f"Payload size {len(payload)} exceeds maximum {protocol.MAX_PAYLOAD_SIZE}")
            envelope.encrypted_payload_with_tag = payload

    body = envelope.SerializeToString()
    return body + (crc32(body) & protocol.CRC32_MASK).to_bytes(4, "little")


def parse_frame(raw_frame_buffer: bytes | bytearray | memoryview, session_key: bytes | None = None) -> DecodedFrame:
    """Parses binary buffer directly into a Protobuf envelope. [SIL-2]"""
    buf = bytes(raw_frame_buffer)
    if len(buf) < _CRC_SIZE:
        raise ValueError("Incomplete frame: too short")

    body, crc_bytes = buf[:-_CRC_SIZE], buf[-_CRC_SIZE:]
    if (crc32(body) & protocol.CRC32_MASK) != int.from_bytes(crc_bytes, "little"):
        raise ValueError("CRC mismatch")

    envelope = pb.RpcEnvelope()
    try:
        envelope.ParseFromString(body)
    except DecodeError as e:
        raise ValueError(f"Failed to parse Protobuf envelope: {e}") from e

    if envelope.version != protocol.PROTOCOL_VERSION:
        raise ValueError("Invalid protocol version")

    is_excluded = is_system_command(envelope.command_id)

    # AEAD Decryption
    if session_key and not is_excluded:
        # Optimization: Use Protobuf envelope itself as AAD by only including header fields.
        aad = pb.RpcEnvelope(
            version=envelope.version, command_id=envelope.command_id, sequence_id=envelope.sequence_id
        ).SerializeToString()

        try:
            assert session_key is not None
            decrypted = ChaCha20Poly1305(session_key).decrypt(envelope.nonce, envelope.encrypted_payload_with_tag, aad)
        except InvalidTag as exc:
            raise ValueError("AEAD decryption failed") from exc
    else:
        # Unencrypted! [SIL-2] Holistic payload extraction from the native oneof field.
        field = envelope.WhichOneof("payload_type")
        if field == "encrypted_payload_with_tag":
            decrypted = envelope.encrypted_payload_with_tag
        elif field:
            decrypted = getattr(envelope, field)
        else:
            decrypted = b""

    return DecodedFrame(envelope=envelope, payload=decrypted)
