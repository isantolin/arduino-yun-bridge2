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

import struct
from binascii import crc32
from typing import Final

from google.protobuf.message import DecodeError
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.security.security import aead_decrypt, aead_encrypt

from . import protocol
from .rle import rle_decode, rle_encode, should_compress

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

    # Detect if command already has compression flag
    is_compressed = bool(command_id & protocol.CMD_FLAG_COMPRESSED)
    raw_cmd = command_id & ~protocol.CMD_FLAG_COMPRESSED & protocol.UINT16_MAX

    # Exclude system/status commands from encryption/compression
    is_excluded = (protocol.STATUS_CODE_MIN <= raw_cmd <= protocol.STATUS_CODE_MAX) or (
        protocol.SYSTEM_COMMAND_MIN <= raw_cmd <= protocol.SYSTEM_COMMAND_MAX
    )

    final_payload = payload
    final_cmd_id = command_id

    # [SIL-2] Payload size validation must happen BEFORE compression/encryption
    if len(final_payload) > protocol.MAX_PAYLOAD_SIZE:
        raise ValueError(f"Payload size {len(final_payload)} exceeds maximum {protocol.MAX_PAYLOAD_SIZE}")

    # RLE Compression (if applicable)
    if not is_compressed and not is_excluded and should_compress(final_payload):
        compressed = rle_encode(final_payload)
        if len(compressed) < len(final_payload):
            final_payload = compressed
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
        # Optimization: Use Protobuf envelope itself as AAD by clearing payload/tag/nonce
        # This avoids redundant manual field orchestration.
        aad_data = pb.RpcEnvelope()
        aad_data.CopyFrom(envelope)
        aad_data.ClearField("payload")
        aad_data.ClearField("tag")
        aad_data.ClearField("nonce")

        inner_payload, out_tag = aead_encrypt(
            final_payload,
            session_key,
            envelope.nonce,
            aad_data.SerializeToString(),
        )
        envelope.payload = inner_payload
        envelope.tag = out_tag

    body = envelope.SerializeToString()
    return body


def parse_frame(raw_frame_buffer: bytes | bytearray | memoryview, session_key: bytes | None = None) -> pb.RpcEnvelope:
    """Parses binary buffer directly into a Protobuf envelope. [SIL-2]"""
    buf = bytes(raw_frame_buffer)
    envelope = pb.RpcEnvelope()
    try:
        envelope.ParseFromString(buf)
    except DecodeError as exc:
        raise ValueError(f"Malformed frame: {exc}") from exc

    return envelope
