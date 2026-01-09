"""RPC frame building and parsing for Arduino-Linux serial communication.

This module implements the binary frame format used over the serial link
between the Linux daemon and the Arduino MCU.

[SIL-2 COMPLIANCE]
The frame format is designed for reliable communication:
- CRC32 integrity check on every frame
- Explicit length fields prevent buffer overruns
- Version field ensures protocol compatibility
- Big-endian byte order for cross-platform consistency

Frame Structure (on wire after COBS encoding):
    [Header (5 bytes)] [Payload (0-128 bytes)] [CRC32 (4 bytes)]

Header Format (big-endian):
    - version (1 byte): Protocol version, must match PROTOCOL_VERSION
    - payload_length (2 bytes): Number of payload bytes
    - command_id (2 bytes): Command or status code from protocol.py

The raw frame is then COBS-encoded and terminated with 0x00 delimiter
before transmission.

Example:
    >>> frame = Frame.build(Command.CMD_GET_VERSION, b"")
    >>> encoded = cobs.encode(frame) + b"\\x00"
"""

import struct
from typing import Self

from . import protocol
from .crc import crc32_ieee


class Frame:
    """Represents an RPC frame for MCU-Linux communication.
    
    This class provides both object-oriented and static methods for
    frame construction and parsing.
    
    Attributes:
        command_id: The RPC command or status code (16-bit).
        payload: The frame payload (0 to MAX_PAYLOAD_SIZE bytes).
    
    Example:
        >>> frame = Frame(Command.CMD_CONSOLE_WRITE, b"Hello")
        >>> raw = frame.to_bytes()  # Build raw frame
        >>> parsed = Frame.from_bytes(raw)  # Parse back
    """
    
    def __init__(self, command_id: int, payload: bytes = b"") -> None:
        """Initialize a Frame instance.
        
        Args:
            command_id: RPC command or status code (0-65535).
            payload: Frame payload bytes (default empty).
        """
        self.command_id = command_id
        self.payload = payload

    @staticmethod
    def build(command_id: int, payload: bytes = b"") -> bytes:
        """Build a raw frame (header + payload + CRC) for COBS encoding."""
        payload_len = len(payload)
        if payload_len > protocol.MAX_PAYLOAD_SIZE:
            raise ValueError(
                f"Payload too large ({payload_len} bytes); "
                f"max is {protocol.MAX_PAYLOAD_SIZE}"
            )
        if not 0 <= command_id <= protocol.UINT16_MAX:
            raise ValueError(f"Command id {command_id} outside 16-bit range")

        # Pack the header that will be part of the CRC calculation
        crc_covered_header = struct.pack(
            protocol.CRC_COVERED_HEADER_FORMAT,
            protocol.PROTOCOL_VERSION,
            payload_len,
            command_id,
        )

        # Calculate CRC over the header and payload, then mask it to the
        # exact number of bits declared by the protocol.
        data_to_crc = crc_covered_header + payload
        crc_mask = (1 << (protocol.CRC_SIZE * 8)) - 1
        crc = crc32_ieee(data_to_crc) & crc_mask

        # Pack the CRC
        crc_packed = struct.pack(
            protocol.CRC_FORMAT,
            crc,
        )

        # Construct the full raw frame
        return crc_covered_header + payload + crc_packed

    @staticmethod
    def parse(raw_frame_buffer: bytes) -> tuple[int, bytes]:
        """Parse a decoded frame and validate header, payload, and CRC."""
        # 1. Verify minimum size
        if len(raw_frame_buffer) < protocol.MIN_FRAME_SIZE:
            raise ValueError(
                "Incomplete frame: size "
                f"{len(raw_frame_buffer)} is less than minimum "
                f"{protocol.MIN_FRAME_SIZE}"
            )

        # 2. Extract and verify CRC
        crc_start = len(raw_frame_buffer) - protocol.CRC_SIZE
        data_to_check = raw_frame_buffer[:crc_start]
        received_crc_packed = raw_frame_buffer[crc_start:]
        (received_crc,) = struct.unpack(
            protocol.CRC_FORMAT,
            received_crc_packed,
        )

        calculated_crc = crc32_ieee(data_to_check)

        if received_crc != calculated_crc:
            raise ValueError(
                f"CRC mismatch. Expected {calculated_crc:08X}, "
                f"got {received_crc:08X}"
            )

        # 3. Extract and validate header
        if len(data_to_check) < protocol.CRC_COVERED_HEADER_SIZE:
            raise ValueError("Incomplete header")

        header_data = data_to_check[: protocol.CRC_COVERED_HEADER_SIZE]
        version, payload_len, command_id = struct.unpack(
            protocol.CRC_COVERED_HEADER_FORMAT, header_data
        )

        if version != protocol.PROTOCOL_VERSION:
            raise ValueError(
                "Invalid version. Expected "
                f"{protocol.PROTOCOL_VERSION}, got {version}"
            )

        # 4. Validate payload length against actual data length
        actual_payload_len = len(data_to_check) - protocol.CRC_COVERED_HEADER_SIZE
        if payload_len != actual_payload_len:
            raise ValueError(
                "Payload length mismatch. Header says "
                f"{payload_len}, but got {actual_payload_len}"
            )

        # 5. Extract payload
        payload = data_to_check[protocol.CRC_COVERED_HEADER_SIZE:]

        return command_id, payload

    def to_bytes(self) -> bytes:
        """Serialize the instance using :meth:`build`."""

        return self.build(self.command_id, self.payload)

    @classmethod
    def from_bytes(cls, raw_frame_buffer: bytes) -> Self:
        """Parse *raw_frame_buffer* and create a :class:`Frame`."""

        command_id, payload = cls.parse(raw_frame_buffer)
        return cls(command_id, payload)
