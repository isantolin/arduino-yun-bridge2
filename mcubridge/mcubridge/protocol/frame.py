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
from typing import Final

from google.protobuf.message import DecodeError
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.security.security import aead_decrypt, aead_encrypt

from . import protocol, is_system_command

_NONCE_SIZE: Final = protocol.AEAD_NONCE_SIZE
_TAG_SIZE: Final = protocol.AEAD_TAG_SIZE
_CRC_SIZE: Final = protocol.CRC_SIZE


def build_frame(
    command_id: int,
    sequence_id: int,
    payload: bytes = b"",
    nonce: bytes | None = None,
    tag: bytes | None = None,
    session_key: bytes | None = None,
) -> bytes:
    """Builds a binary frame using a Protobuf envelope directly. [SIL-2]"""
    if not (0 <= command_id <= protocol.UINT16_MAX):
        raise ValueError(f"Invalid command ID: {command_id}")

    is_excluded = is_system_command(command_id)

    # [SIL-2] Payload size validation must happen BEFORE encryption
    if len(payload) > protocol.MAX_PAYLOAD_SIZE:
        raise ValueError(f"Payload size {len(payload)} exceeds maximum {protocol.MAX_PAYLOAD_SIZE}")

    # Initialize RpcEnvelope directly
    envelope = pb.RpcEnvelope(
        version=protocol.PROTOCOL_VERSION,
        command_id=command_id,
        sequence_id=sequence_id,
        nonce=nonce or (b"\x00" * _NONCE_SIZE),
        tag=tag or (b"\x00" * _TAG_SIZE),
        payload=payload,
    )

    # AEAD Encryption (if session key provided)
    if session_key and not is_excluded:
        # Optimization: Use Protobuf envelope itself as AAD by only including header fields.
        aad = pb.RpcEnvelope(
            version=envelope.version, command_id=envelope.command_id, sequence_id=envelope.sequence_id
        ).SerializeToString()

        envelope.payload, envelope.tag = aead_encrypt(
            payload,
            session_key,
            envelope.nonce,
            aad,
        )

    body = envelope.SerializeToString()
    return body + (crc32(body) & protocol.CRC32_MASK).to_bytes(4, "little")


def parse_frame(raw_frame_buffer: bytes | bytearray | memoryview, session_key: bytes | None = None) -> pb.RpcEnvelope:
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

        envelope.payload = aead_decrypt(
            envelope.payload,
            envelope.tag,
            session_key,
            envelope.nonce,
            aad,
        )

    return envelope
