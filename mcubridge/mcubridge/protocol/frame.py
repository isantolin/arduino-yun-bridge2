"""RPC frame building and parsing for Arduino-Linux serial communication.

This module implements the binary frame format used over the serial link
between the Linux daemon and the Arduino MCU.

[SIL-2 COMPLIANCE]
The frame format is strictly defined to ensure:
- Deterministic memory layout using stdlib struct (C-backed).
- Explicit CRC32 validation.
- Zero boilerplate compatibility layers.
"""

from __future__ import annotations

import struct
from binascii import crc32
from typing import Final

import msgspec

from . import protocol

# [SIL-2] Frame Header Format: version(B), payload_len(H), command_id(H), sequence_id(H)
# Big-endian (>) is mandatory for network/serial protocol integrity.
_HEADER_FMT: Final[str] = ">BHHH"
_HEADER_SIZE: Final[int] = struct.calcsize(_HEADER_FMT)
_CRC_FMT: Final[str] = ">I"
_CRC_SIZE: Final[int] = struct.calcsize(_CRC_FMT)


class Frame(msgspec.Struct, frozen=True):
    """Represents an RPC frame for MCU-Linux communication."""

    command_id: int | protocol.Command | protocol.Status
    sequence_id: int
    payload: bytes = b""

    def __iter__(self):
        """Allow unpacking: cmd, seq, payload = frame."""
        yield self.command_id
        yield self.sequence_id
        yield self.payload

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
        """Build the binary frame representation with explicit RLE compression."""
        from . import rle

        if len(self.payload) > protocol.MAX_PAYLOAD_SIZE:
            raise ValueError(
                f"Payload too large: {len(self.payload)} > {protocol.MAX_PAYLOAD_SIZE}"
            )

        cmd_id = int(self.command_id)
        working_payload = self.payload

        # [SIL-2] Explicit Compression Logic
        if working_payload and rle.should_compress(working_payload):
            compressed = rle.RLE_TRANSFORM.build(working_payload)
            if len(compressed) < len(working_payload):
                working_payload = compressed
                cmd_id |= protocol.CMD_FLAG_COMPRESSED

        # 1. Build Header
        header = struct.pack(
            _HEADER_FMT,
            protocol.PROTOCOL_VERSION,
            len(working_payload),
            cmd_id,
            self.sequence_id,
        )

        # 2. Body for CRC (Header + Payload)
        body = header + working_payload

        # 3. Calculate and Append CRC32
        crc = crc32(body) & 0xFFFFFFFF
        return body + struct.pack(_CRC_FMT, crc)

    @classmethod
    def parse(cls, raw_frame_buffer: bytes | bytearray | memoryview) -> "Frame":
        """Parse *raw_frame_buffer* and create a :class:`Frame` with explicit RLE decompression."""
        if len(raw_frame_buffer) < (_HEADER_SIZE + _CRC_SIZE):
            raise ValueError("Frame too short")

        # 1. Split parts
        header_raw = raw_frame_buffer[:_HEADER_SIZE]
        version, payload_len, cmd_id, seq_id = struct.unpack(_HEADER_FMT, header_raw)

        if version != protocol.PROTOCOL_VERSION:
            raise ValueError(f"Protocol version mismatch: {version}")

        # Check total length
        expected_len = _HEADER_SIZE + payload_len + _CRC_SIZE
        if len(raw_frame_buffer) < expected_len:
            raise ValueError(
                f"Incomplete frame: got {len(raw_frame_buffer)}, expected {expected_len}"
            )

        payload_raw = raw_frame_buffer[_HEADER_SIZE : _HEADER_SIZE + payload_len]
        crc_raw = raw_frame_buffer[_HEADER_SIZE + payload_len : expected_len]

        # 2. Verify CRC32
        body = header_raw + payload_raw
        calculated_crc = crc32(body) & 0xFFFFFFFF
        (received_crc,) = struct.unpack(_CRC_FMT, crc_raw)

        if calculated_crc != received_crc:
            raise ValueError(
                f"CRC mismatch: calculated {calculated_crc:08X}, received {received_crc:08X}"
            )

        # 3. Decompression
        payload = bytes(payload_raw)
        if cmd_id & protocol.CMD_FLAG_COMPRESSED:
            from .rle import RLE_TRANSFORM

            payload = RLE_TRANSFORM.parse(payload)
            cmd_id &= ~protocol.CMD_FLAG_COMPRESSED

        return cls(
            command_id=cmd_id,
            sequence_id=seq_id,
            payload=payload,
        )
