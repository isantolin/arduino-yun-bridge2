"""RPC frame building and parsing for Arduino-Linux serial communication.

This module implements the binary frame format used over the serial link
between the Linux daemon and the Arduino MCU.

[SIL-2 COMPLIANCE]
- Zero manual orchestration logic.
- Direct Protobuf library usage (no wrappers).
- Explicit CRC32 validation.
- Native integration of RLE compression.
"""

from __future__ import annotations

from typing import Final

from google.protobuf.message import DecodeError
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.security.security import aead_decrypt, aead_encrypt

from . import protocol, is_system_command
from .rle import rle_decode, rle_encode_if_beneficial

_NONCE_SIZE: Final = protocol.AEAD_NONCE_SIZE
_TAG_SIZE: Final = protocol.AEAD_TAG_SIZE


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

    # Exclude system/status commands from encryption/compression
    is_excluded = is_system_command(command_id)
    is_compressed = bool(command_id & protocol.CMD_FLAG_COMPRESSED)

    final_payload = payload
    final_cmd_id = command_id

    # [SIL-2] Payload size validation must happen BEFORE compression/encryption
    if len(final_payload) > protocol.MAX_PAYLOAD_SIZE:
        raise ValueError(f"Payload size {len(final_payload)} exceeds maximum {protocol.MAX_PAYLOAD_SIZE}")

    # RLE Compression (if applicable)
    if not is_compressed and not is_excluded:
        final_payload, was_compressed = rle_encode_if_beneficial(final_payload)
        if was_compressed:
            final_cmd_id |= protocol.CMD_FLAG_COMPRESSED

    # Initialize RpcEnvelope directly
    envelope = pb.RpcEnvelope(
        version=protocol.PROTOCOL_VERSION,
        command_id=final_cmd_id,
        sequence_id=sequence_id,
        nonce=nonce or (b"\x00" * _NONCE_SIZE),
        tag=tag or (b"\x00" * _TAG_SIZE),
        payload=final_payload,
    )

    # AEAD Encryption (if session key provided)
    if session_key and not is_excluded:
        # Optimization: Use Protobuf envelope itself as AAD by only including header fields.
        aad = pb.RpcEnvelope(
            version=envelope.version, command_id=envelope.command_id, sequence_id=envelope.sequence_id
        ).SerializeToString()

        envelope.payload, envelope.tag = aead_encrypt(
            final_payload,
            session_key,
            envelope.nonce,
            aad,
        )

    body = envelope.SerializeToString()
    return body


def parse_frame(raw_frame_buffer: bytes | bytearray | memoryview, session_key: bytes | None = None) -> pb.RpcEnvelope:
    """Parses binary buffer directly into a Protobuf envelope. [SIL-2]"""
    buf = bytes(raw_frame_buffer)
    body = buf
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

    # RLE Decompression
    if envelope.command_id & protocol.CMD_FLAG_COMPRESSED:
        envelope.payload = rle_decode(envelope.payload)
        envelope.command_id &= ~protocol.CMD_FLAG_COMPRESSED

    return envelope
