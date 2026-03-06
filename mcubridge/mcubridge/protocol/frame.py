"""RPC frame building and parsing for Arduino-Linux serial communication."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from .structures import FRAME_STRUCT, MIN_FRAME_SIZE, UINT16_STRUCT

logger = logging.getLogger("mcubridge.frame")

# Protocol constants
PROTOCOL_VERSION: Final[int] = 0x02


@dataclass(frozen=True)
class Frame:
    """Represents a single RPC frame."""

    command_id: int
    payload: bytes = b""
    version: int = PROTOCOL_VERSION

    @classmethod
    def build(cls, command_id: int, payload: bytes = b"") -> bytes:
        """Build a raw binary frame including CRC32."""
        import zlib
        
        header = FRAME_STRUCT.build(PROTOCOL_VERSION, command_id, len(payload))
        data_to_crc = header + payload
        crc = zlib.crc32(data_to_crc) & 0xFFFFFFFF
        return data_to_crc + struct.pack(">I", crc)

    @classmethod
    def parse(cls, raw_frame_buffer: bytes | bytearray | memoryview) -> tuple[int, bytes]:
        """Parse a raw binary frame and verify CRC32."""
        import zlib
        import struct
        
        data = bytes(raw_frame_buffer)
        if len(data) < MIN_FRAME_SIZE:
            raise ValueError(f"Frame too short: {len(data)} bytes")

        header_bytes = data[:FRAME_STRUCT.size]
        version, command_id, payload_len = FRAME_STRUCT.parse(header_bytes)

        if version != PROTOCOL_VERSION:
            raise ValueError(f"Unsupported protocol version: 0x{version:02X}")

        expected_size = FRAME_STRUCT.size + payload_len + 4
        if len(data) < expected_size:
            raise ValueError(f"Frame truncated: expected {expected_size}, got {len(data)}")

        payload = data[FRAME_STRUCT.size : FRAME_STRUCT.size + payload_len]
        received_crc = struct.unpack(">I", data[FRAME_STRUCT.size + payload_len : expected_size])[0]
        
        calculated_crc = zlib.crc32(data[: FRAME_STRUCT.size + payload_len]) & 0xFFFFFFFF
        if received_crc != calculated_crc:
            raise ValueError(f"CRC mismatch: received 0x{received_crc:08X}, calculated 0x{calculated_crc:08X}")

        return command_id, payload

    @classmethod
    def from_bytes(cls, raw_frame_buffer: bytes | bytearray | memoryview) -> "Frame":
        """Parse *raw_frame_buffer* and create a :class:`Frame`."""
        command_id, payload = cls.parse(raw_frame_buffer)
        return cls(command_id=command_id, payload=payload)


import struct # For building
