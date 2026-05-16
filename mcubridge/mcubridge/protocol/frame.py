"""RPC frame building and parsing for Arduino-Linux serial communication.

This module implements the binary frame format used over the serial link
between the Linux daemon and the Arduino MCU using declarative msgspec structures.

[SIL-2 COMPLIANCE]
The frame format is strictly defined to ensure:
- Deterministic memory layout.
- Explicit CRC32 validation.
- Zero manual orchestration logic.
- Native integration of RLE compression.
"""

from __future__ import annotations

import struct
from binascii import crc32

import msgspec

from . import protocol
from .rle import rle_encode, rle_decode, should_compress

_HEADER_FORMAT = protocol.FRAME_HEADER_FORMAT
HEADER_STRUCT = struct.Struct(_HEADER_FORMAT)
_HEADER_SIZE = HEADER_STRUCT.size
_NONCE_SIZE = protocol.AEAD_NONCE_SIZE
_TAG_SIZE = protocol.AEAD_TAG_SIZE
CRC_STRUCT = struct.Struct(protocol.FRAME_CRC_FORMAT)
_CRC_SIZE = CRC_STRUCT.size


def _frame_crc(data: bytes | bytearray | memoryview) -> int:
    """CRC32 checksum for frame integrity (SIL-2)."""
    return crc32(data) & protocol.CRC32_MASK


class Frame(msgspec.Struct, frozen=True):
    """Represents an RPC frame for MCU-Linux communication."""

    command_id: int | protocol.Command | protocol.Status
    sequence_id: int
    payload: bytes = b""
    nonce: bytes = b"\x00" * _NONCE_SIZE
    tag: bytes = b"\x00" * _TAG_SIZE
    header_bytes: bytes | None = None

    def __iter__(self):
        """Allow unpacking: cmd, seq, payload = frame."""
        yield self.command_id
        yield self.sequence_id
        yield self.payload
        yield self.nonce
        yield self.tag

    @property
    def is_compressed(self) -> bool:
        """Check if the frame payload is compressed (bit 15)."""
        return bool(int(self.command_id) & protocol.CMD_FLAG_COMPRESSED)

    @property
    def raw_command_id(self) -> int:
        """Get the raw 15-bit command ID without the compression flag."""
        return (
            int(self.command_id) & ~protocol.CMD_FLAG_COMPRESSED & protocol.UINT16_MAX
        )

    def build(self) -> bytes:
        """Delegates frame building to the declarative schema."""
        cmd_id = int(self.command_id)
        payload = self.payload

        if len(payload) > protocol.MAX_PAYLOAD_SIZE:
            raise ValueError(
                f"Payload size {len(payload)} exceeds maximum {protocol.MAX_PAYLOAD_SIZE}"
            )

        if (
            payload
            and cmd_id
            not in (
                protocol.Command.CMD_LINK_SYNC,
                protocol.Command.CMD_LINK_RESET,
                protocol.Command.CMD_SET_BAUDRATE,
                protocol.Command.CMD_GET_CAPABILITIES,
            )
            and should_compress(payload)
        ):
            compressed = rle_encode(payload)
            if len(compressed) < len(payload):
                payload = compressed
                cmd_id |= protocol.CMD_FLAG_COMPRESSED

        try:
            header = HEADER_STRUCT.pack(
                protocol.PROTOCOL_VERSION,
                len(payload),
                cmd_id,
                self.sequence_id,
            )
        except struct.error as e:
            raise ValueError(f"Failed to build frame: {e}") from e

        body = header + self.nonce + payload + self.tag
        crc = _frame_crc(body)

        return body + CRC_STRUCT.pack(crc)

    @classmethod
    def parse(cls, raw_frame_buffer: bytes | bytearray | memoryview) -> "Frame":
        """Delegates frame parsing to the declarative schema."""
        buf = memoryview(raw_frame_buffer)
        if len(buf) < _HEADER_SIZE + _NONCE_SIZE + _TAG_SIZE + _CRC_SIZE:
            raise ValueError("Incomplete or malformed frame: too short")

        body_len = len(buf) - _CRC_SIZE
        body = buf[:body_len]
        try:
            expected_crc = CRC_STRUCT.unpack(buf[body_len:])[0]
        except struct.error as e:
            raise ValueError(f"Malformed CRC field: {e}") from e

        actual_crc = _frame_crc(body)

        if expected_crc != actual_crc:
            raise ValueError(f"CRC mismatch: expected {expected_crc}, got {actual_crc}")

        try:
            version, payload_len, cmd_id, seq_id = HEADER_STRUCT.unpack(
                body[:_HEADER_SIZE]
            )
        except struct.error as e:
            raise ValueError(f"Malformed header: {e}") from e

        if version != protocol.PROTOCOL_VERSION:
            raise ValueError("Incomplete or malformed frame: invalid version")

        if _HEADER_SIZE + _NONCE_SIZE + payload_len + _TAG_SIZE != body_len:
            raise ValueError("Incomplete or malformed frame: invalid length")

        nonce = bytes(body[_HEADER_SIZE : _HEADER_SIZE + _NONCE_SIZE])
        payload = bytes(
            body[_HEADER_SIZE + _NONCE_SIZE : _HEADER_SIZE + _NONCE_SIZE + payload_len]
        )
        tag = bytes(body[_HEADER_SIZE + _NONCE_SIZE + payload_len : body_len])

        if cmd_id & protocol.CMD_FLAG_COMPRESSED:
            try:
                payload = rle_decode(payload)
                cmd_id &= ~protocol.CMD_FLAG_COMPRESSED
            except ValueError as e:
                raise ValueError(
                    f"Incomplete or malformed frame: RLE decode failed: {e}"
                ) from e

        return cls(
            command_id=cmd_id,
            sequence_id=seq_id,
            payload=payload,
            nonce=nonce,
            tag=tag,
            header_bytes=bytes(body[:_HEADER_SIZE]),
        )
