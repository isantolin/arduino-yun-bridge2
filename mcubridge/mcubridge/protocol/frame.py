"""RPC frame building and parsing for Arduino-Linux serial communication.

This module implements the binary frame format used over the serial link
between the Linux daemon and the Arduino MCU.

[SIL-2 / MIL-SPEC OPTIMIZATIONS]
- LRU caching of ChaCha20Poly1305 cipher contexts per session key to eliminate OpenSSL re-allocations.
- Direct binary Varint AAD builder (_build_aad_bytes) replacing redundant Protobuf envelope instantiations.
- Zero-copy memoryview parsing and fast CRC32 verification using binascii C extension.
"""

from __future__ import annotations
from google.protobuf.internal.encoder import _VarintBytes  # type: ignore[import-untyped]

import struct
from binascii import crc32
from functools import lru_cache
from typing import Callable, Final, NamedTuple, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from mcubridge.protocol import protocol_pb2 as pb

# ═════════════════════════════════════════════════════════════════════════════
# FRAME PROTOCOL CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════
FRAME_HEADER_MAGIC: Final[int] = 0xAA
FRAME_HEADER_SIZE: Final[int] = 5  # Magic(1) + Version(1) + Cmd(1) + Seq(1) + PayloadLen(1)
FRAME_CRC_SIZE: Final[int] = 4
FRAME_MIN_SIZE: Final[int] = FRAME_HEADER_SIZE + FRAME_CRC_SIZE
PROTOCOL_VERSION: Final[int] = 1

# Mask for CRC32 calculations to ensure unsigned 32-bit integer representation
CRC32_MASK: Final[int] = 0xFFFFFFFF


@lru_cache(maxsize=16)
def _get_cipher(session_key: bytes) -> ChaCha20Poly1305:
    """Cache ChaCha20Poly1305 cipher instances to eliminate OpenSSL C-extension re-allocations per frame. [SIL-2]"""
    return ChaCha20Poly1305(session_key)


_encode_varint: Callable[[int], bytes] = cast(Callable[[int], bytes], _VarintBytes)


def _build_aad_bytes(version: int, command_id: int, sequence_id: int) -> bytes:
    """Fast binary Protobuf Varint encoder for RpcEnvelope AAD (fields 1, 2, 3). [SIL-2]"""
    return b"".join(
        (
            b"\x08",
            _encode_varint(version),
            b"\x10",
            _encode_varint(command_id),
            b"\x18",
            _encode_varint(sequence_id),
        )
    )


class ParsedFrame(NamedTuple):
    """Immutable representation of a validated binary frame parsed from the serial link."""

    version: int
    command_id: int
    sequence_id: int
    payload: bytes
    raw: bytes


class FrameError(Exception):
    """Base exception for all frame decoding/verification failures."""


class HeaderError(FrameError):
    """Raised when the frame magic byte or version check fails."""


class IntegrityError(FrameError):
    """Raised when CRC32 checksum or AEAD tag verification fails."""


class DecodeError(FrameError):
    """Raised when Protobuf payload deserialization fails."""


def build_frame(
    command_id: int,
    sequence_id: int,
    payload: bytes = b"",
    *,
    version: int = PROTOCOL_VERSION,
    session_key: bytes | None = None,
) -> bytes:
    """Build a framed binary packet with CRC32 integrity check and optional AEAD encryption."""
    if not (0 <= command_id <= 255):
        raise ValueError(f"Command ID out of range 0..255: {command_id}")
    if not (0 <= sequence_id <= 255):
        raise ValueError(f"Sequence ID out of range 0..255: {sequence_id}")
    if not (0 <= version <= 255):
        raise ValueError(f"Protocol version out of range 0..255: {version}")

    aad = _build_aad_bytes(version, command_id, sequence_id)

    if session_key is not None:
        cipher = _get_cipher(session_key)
        # Nonce format: 4 bytes sequence + 8 zero bytes = 12 bytes
        nonce = sequence_id.to_bytes(4, byteorder="big") + (b"\x00" * 8)
        payload = cipher.encrypt(nonce, payload, aad)

    payload_len = len(payload)
    if payload_len > 255:
        raise ValueError(f"Payload length exceeds maximum allowable frame size (255): {payload_len}")

    header = struct.pack(">BBBBB", FRAME_HEADER_MAGIC, version, command_id, sequence_id, payload_len)
    data_to_checksum = header + payload
    checksum = crc32(data_to_checksum) & CRC32_MASK
    return data_to_checksum + struct.pack(">I", checksum)


def parse_frame(
    raw_frame: bytes | memoryview,
    *,
    expected_version: int = PROTOCOL_VERSION,
    session_key: bytes | None = None,
) -> ParsedFrame:
    """Parse and validate a binary frame from serial data."""
    frame_view = memoryview(raw_frame) if not isinstance(raw_frame, memoryview) else raw_frame
    frame_len = len(frame_view)

    if frame_len < FRAME_MIN_SIZE:
        raise HeaderError(f"Frame length {frame_len} is smaller than minimum header size ({FRAME_MIN_SIZE})")

    magic, version, command_id, sequence_id, payload_len = struct.unpack_from(">BBBBB", frame_view, 0)

    if magic != FRAME_HEADER_MAGIC:
        raise HeaderError(f"Invalid frame magic byte: 0x{magic:02X} (expected 0x{FRAME_HEADER_MAGIC:02X})")

    if version != expected_version:
        raise HeaderError(f"Unsupported protocol version: {version} (expected {expected_version})")

    expected_total_len = FRAME_HEADER_SIZE + payload_len + FRAME_CRC_SIZE
    if frame_len != expected_total_len:
        raise HeaderError(
            f"Frame size mismatch: buffer has {frame_len} bytes, header specifies {expected_total_len}"
        )

    expected_crc = struct.unpack_from(">I", frame_view, FRAME_HEADER_SIZE + payload_len)[0]
    data_view = frame_view[: FRAME_HEADER_SIZE + payload_len]
    actual_crc = crc32(data_view) & CRC32_MASK

    if actual_crc != expected_crc:
        raise IntegrityError(f"CRC32 checksum mismatch: got 0x{actual_crc:08X}, expected 0x{expected_crc:08X}")

    payload = bytes(frame_view[FRAME_HEADER_SIZE : FRAME_HEADER_SIZE + payload_len])

    if session_key is not None:
        cipher = _get_cipher(session_key)
        nonce = sequence_id.to_bytes(4, byteorder="big") + (b"\x00" * 8)
        aad = _build_aad_bytes(version, command_id, sequence_id)
        try:
            payload = cipher.decrypt(nonce, payload, aad)
        except InvalidTag as exc:
            raise IntegrityError("AEAD decryption failed: invalid authentication tag or corrupted frame") from exc

    return ParsedFrame(
        version=version,
        command_id=command_id,
        sequence_id=sequence_id,
        payload=payload,
        raw=bytes(frame_view),
    )


def decode_envelope(payload: bytes) -> pb.RpcEnvelope:
    """Decode a Protobuf RpcEnvelope from a parsed frame payload."""
    envelope = pb.RpcEnvelope()
    try:
        envelope.ParseFromString(payload)
    except Exception as exc:
        raise DecodeError(f"Failed to decode RpcEnvelope protobuf: {exc}") from exc

    return envelope
