"""RPC frame building and parsing for Arduino-Linux serial communication.

This module implements the binary frame format used over the serial link
between the Linux daemon and the Arduino MCU.

[SIL-2 COMPLIANCE]
- Zero manual orchestration logic.
- Direct Protobuf library usage (no wrappers).
- Explicit CRC32 validation.
"""

from __future__ import annotations
from cobs import cobs

import struct
from binascii import crc32
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from google.protobuf.message import DecodeError, Message as ProtobufMessage
import msgspec

from mcubridge.protocol import mcubridge_pb2 as pb

from . import protocol, is_system_command

_NONCE_SIZE: Final = protocol.AEAD_NONCE_SIZE
_TAG_SIZE: Final = protocol.AEAD_TAG_SIZE
_CRC_SIZE: Final = protocol.CRC_SIZE


class DecodedFrame(msgspec.Struct):
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
        tag=tag or (b"\x00" * _TAG_SIZE),
    )

    payload_bytes = payload.SerializeToString() if isinstance(payload, ProtobufMessage) else payload
    if len(payload_bytes) > protocol.MAX_PAYLOAD_SIZE:
        raise ValueError(f"Payload size {len(payload_bytes)} exceeds maximum {protocol.MAX_PAYLOAD_SIZE}")

    # AEAD Encryption (if session key provided)
    do_encrypt = session_key and not is_excluded
    if do_encrypt:
        assert session_key is not None
        # Optimization: Use Protobuf envelope itself as AAD by only including header fields.
        aad = pb.RpcEnvelope(
            version=envelope.version, command_id=envelope.command_id, sequence_id=envelope.sequence_id
        ).SerializeToString()

        full_ct = ChaCha20Poly1305(session_key).encrypt(envelope.nonce, payload_bytes, aad)
        envelope.encrypted_payload, envelope.tag = full_ct[:-16], full_ct[-16:]
    else:
        # [SIL-2] Direct payload assignment eradicating redundant envelope field logic.
        envelope.encrypted_payload = payload_bytes

    body = envelope.SerializeToString()
    full_frame = body + struct.pack("<I", crc32(body) & protocol.CRC32_MASK)
    return cobs.encode(full_frame) + protocol.FRAME_DELIMITER


def parse_frame(encoded_buffer: bytes | bytearray | memoryview, session_key: bytes | None = None) -> DecodedFrame:
    """Parses binary buffer directly into a Protobuf envelope. [SIL-2]"""
    # [SIL-2] De-layered: COBS decoding integrated into protocol layer.
    raw = bytes(encoded_buffer)
    if raw.endswith(b"\x00"):
        raw = raw[:-1]
    if not raw:
        raise ValueError("Empty frame after stripping delimiter")
    try:
        buf = cobs.decode(raw)
    except cobs.DecodeError as e:
        raise ValueError(f"COBS decode failed: {e}") from e
    if len(buf) < _CRC_SIZE:
        raise ValueError("Incomplete frame: too short")

    body, crc_bytes = buf[:-_CRC_SIZE], buf[-_CRC_SIZE:]
    if (crc32(body) & protocol.CRC32_MASK) != struct.unpack("<I", crc_bytes)[0]:
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
            decrypted = ChaCha20Poly1305(session_key).decrypt(
                envelope.nonce, envelope.encrypted_payload + envelope.tag, aad
            )
        except InvalidTag as exc:
            raise ValueError("AEAD decryption failed") from exc
    else:
        # Unencrypted! [SIL-2] Holistic payload extraction from the unified encrypted_payload field.
        decrypted = envelope.encrypted_payload

    return DecodedFrame(envelope=envelope, payload=decrypted)
