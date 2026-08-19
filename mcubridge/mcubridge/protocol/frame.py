"""RPC frame building and parsing for Arduino-Linux serial communication.

This module implements the binary frame format used over the serial link
between the Linux daemon and the Arduino MCU.

[SIL-2 / MIL-SPEC OPTIMIZATIONS]
- LRU caching of ChaCha20Poly1305 cipher contexts per session key to eliminate OpenSSL re-allocations.
- Standard Protobuf serialization for AEAD AAD construction (RpcEnvelope header fields). [SIL-2]
- Zero-copy memoryview parsing and fast CRC32 verification using binascii C extension.
"""

from __future__ import annotations

from binascii import crc32
from functools import lru_cache
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from google.protobuf.message import DecodeError, Message as ProtobufMessage

from mcubridge.protocol import mcubridge_pb2 as pb
from . import is_system_command, protocol

_NONCE_SIZE: Final = protocol.AEAD_NONCE_SIZE
_TAG_SIZE: Final = protocol.AEAD_TAG_SIZE
_CRC_SIZE: Final = protocol.CRC_SIZE


@lru_cache(maxsize=16)
def _get_cipher(session_key: bytes) -> ChaCha20Poly1305:
    """Cache ChaCha20Poly1305 cipher instances to eliminate OpenSSL C-extension re-allocations per frame. [SIL-2]"""
    return ChaCha20Poly1305(session_key)


@lru_cache(maxsize=4096)
def _build_aad_bytes(version: int, command_id: int, sequence_id: int) -> bytes:
    """Build AEAD AAD via fast direct varint encoding and cached Protobuf serialization. [SIL-2]"""
    if version == 1:
        if command_id < 0x80 and sequence_id < 0x80:
            return bytes((0x08, 0x01, 0x10, command_id, 0x18, sequence_id))
        if command_id < 0x80 and sequence_id < 0x4000:
            return bytes((0x08, 0x01, 0x10, command_id, 0x18, (sequence_id & 0x7F) | 0x80, sequence_id >> 7))
        if command_id < 0x4000 and sequence_id < 0x80:
            return bytes((0x08, 0x01, 0x10, (command_id & 0x7F) | 0x80, command_id >> 7, 0x18, sequence_id))
        if command_id < 0x4000 and sequence_id < 0x4000:
            return bytes(
                (
                    0x08,
                    0x01,
                    0x10,
                    (command_id & 0x7F) | 0x80,
                    command_id >> 7,
                    0x18,
                    (sequence_id & 0x7F) | 0x80,
                    sequence_id >> 7,
                )
            )

    return pb.RpcEnvelope(
        version=version,
        command_id=command_id,
        sequence_id=sequence_id,
    ).SerializeToString()


class DecodedFrame:
    """High-performance decoded frame structure with zero NamedTuple overhead. [SIL-2]"""

    __slots__ = ("envelope", "payload")

    def __init__(self, envelope: pb.RpcEnvelope, payload: bytes | ProtobufMessage) -> None:
        self.envelope = envelope
        self.payload = payload

    def __iter__(self):
        yield self.envelope
        yield self.payload

    def __getitem__(self, index: int) -> pb.RpcEnvelope | bytes | ProtobufMessage:
        if index == 0:
            return self.envelope
        if index == 1:
            return self.payload
        raise IndexError("DecodedFrame index out of range")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, DecodedFrame):
            return self.envelope == other.envelope and self.payload == other.payload
        if isinstance(other, tuple) and len(other) == 2:
            return self.envelope == other[0] and self.payload == other[1]
        return False

    __hash__ = None


def build_frame(
    command_id: int,
    sequence_id: int,
    payload: bytes | ProtobufMessage = b"",
    nonce: bytes | None = None,
    tag: bytes | None = None,
    session_key: bytes | None = None,
) -> bytes:
    """Builds a binary frame using a Protobuf envelope directly with high-performance AEAD. [SIL-2]

    [CRITICAL PERFORMANCE CONSTRAINT - HOT PATH]
    - Must maintain sub-microsecond to low-microsecond latency (< 10 µs / > 150,000 ops/sec).
    - Fast native integer bounds checks combined with C-extension Protobuf serialization guarantee
      both deterministic SIL-2 memory safety and maximum throughput.
    """
    if not (0 <= command_id <= protocol.UINT16_MAX):
        raise ValueError(f"Invalid command ID: {command_id}")
    if not (0 <= sequence_id <= protocol.UINT16_MAX):
        raise ValueError(f"Invalid sequence ID: {sequence_id}")

    is_excluded = is_system_command(command_id)

    # Initialize RpcEnvelope directly (Protobuf rejects negative uint32 automatically)
    envelope = pb.RpcEnvelope(
        version=protocol.PROTOCOL_VERSION,
        command_id=command_id,
        sequence_id=sequence_id,
        nonce=nonce or protocol.ZERO_NONCE,
    )

    # AEAD Encryption (if session key provided)
    do_encrypt = bool(session_key and not is_excluded)

    if do_encrypt:
        if session_key is None:
            raise ValueError("AEAD session key is required for encryption")
        payload_bytes = payload.SerializeToString() if isinstance(payload, ProtobufMessage) else payload
        if len(payload_bytes) > protocol.MAX_PAYLOAD_SIZE:
            raise ValueError(f"Payload size {len(payload_bytes)} exceeds maximum {protocol.MAX_PAYLOAD_SIZE}")

        # Optimization: Fast Varint AAD binary builder + Cached ChaCha20Poly1305 cipher
        aad = _build_aad_bytes(envelope.version, envelope.command_id, envelope.sequence_id)
        cipher = _get_cipher(session_key)
        envelope.encrypted_payload_with_tag = cipher.encrypt(envelope.nonce, payload_bytes, aad)
    else:
        # Unencrypted! [SIL-2] Holistic payload extraction natively handled by Protobuf.
        if isinstance(payload, ProtobufMessage):
            # [SIL-2] Use descriptor-based field mapping to eliminate manual string logic.
            field_name = protocol.PAYLOAD_FIELD_MAP.get(payload.DESCRIPTOR.name)
            if field_name:
                getattr(envelope, field_name).CopyFrom(payload)
        else:
            if len(payload) > protocol.MAX_PAYLOAD_SIZE:
                raise ValueError(f"Payload size {len(payload)} exceeds maximum {protocol.MAX_PAYLOAD_SIZE}")
            envelope.encrypted_payload_with_tag = payload

    body = envelope.SerializeToString()
    crc = crc32(body) & protocol.CRC32_MASK
    return body + crc.to_bytes(4, "little")


def parse_frame(raw_frame_buffer: bytes | bytearray | memoryview, session_key: bytes | None = None) -> DecodedFrame:
    """Parses binary buffer directly into a Protobuf envelope using zero-copy memoryview. [SIL-2]

    [CRITICAL PERFORMANCE CONSTRAINT - HOT PATH]
    - Must maintain sub-microsecond to low-microsecond latency (< 10 µs / > 100,000 ops/sec).
    - Fast native version verification and C-extension Protobuf parsing guarantee deterministic,
      safe, and high-frequency serial packet deserialization.
    """
    buf_len = len(raw_frame_buffer)
    if buf_len < _CRC_SIZE:
        raise ValueError("Incomplete frame: too short")

    body = raw_frame_buffer[:-_CRC_SIZE]
    crc_bytes = raw_frame_buffer[-_CRC_SIZE:]
    expected_crc = int.from_bytes(crc_bytes, "little")
    actual_crc = crc32(body) & protocol.CRC32_MASK
    if actual_crc != expected_crc:
        raise ValueError("CRC mismatch")

    envelope = pb.RpcEnvelope()
    try:
        envelope.ParseFromString(body if isinstance(body, bytes) else bytes(body))
    except DecodeError as e:
        raise ValueError(f"Failed to parse Protobuf envelope: {e}") from e

    if envelope.version != protocol.PROTOCOL_VERSION:
        raise ValueError(f"Unsupported protocol version: {envelope.version}")

    is_excluded = is_system_command(envelope.command_id)

    # AEAD Decryption
    if session_key and not is_excluded:
        # Optimization: Fast Varint AAD binary builder + Cached ChaCha20Poly1305 cipher
        aad = _build_aad_bytes(envelope.version, envelope.command_id, envelope.sequence_id)
        cipher = _get_cipher(session_key)

        try:
            decrypted = cipher.decrypt(envelope.nonce, envelope.encrypted_payload_with_tag, aad)
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
