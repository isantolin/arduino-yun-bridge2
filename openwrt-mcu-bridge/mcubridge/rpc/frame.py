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
    >>> encoded = cobs.encode(frame) + bytes([0])
"""

import msgspec
from binascii import crc32
from typing import Any, cast

from construct import Bytes, Int8ub, Int16ub, Struct, Terminated, this  # type: ignore

from . import protocol


# Define the binary structure of the frame (excluding CRC trailer)
# Corresponds to: [Version:1][PayloadLen:2][CommandId:2][Payload:N]
HeaderPayloadStruct: Any = Struct(
    cast(Any, "version") / Int8ub,
    cast(Any, "payload_len") / Int16ub,
    cast(Any, "command_id") / Int16ub,
    cast(Any, "payload") / Bytes(this.payload_len),
    Terminated,
)


class Frame(msgspec.Struct):
    """Represents an RPC frame for MCU-Linux communication.

    This class provides both object-oriented and static methods for
    frame construction and parsing.

    Attributes:
        command_id: The RPC command or status code (16-bit).
        payload: The frame payload (0 to MAX_PAYLOAD_SIZE bytes).

    Example:
        >>> frame = Frame(command_id=Command.CMD_CONSOLE_WRITE, payload=b"Hello")
        >>> raw = frame.to_bytes()  # Build raw frame
        >>> parsed = Frame.from_bytes(raw)  # Parse back
    """

    command_id: int
    payload: bytes = b""

    @staticmethod
    def build(command_id: int, payload: bytes = b"") -> bytes:
        """Build a raw frame (header + payload + CRC) for COBS encoding."""
        payload_len = len(payload)
        if payload_len > protocol.MAX_PAYLOAD_SIZE:
            raise ValueError(f"Payload too large ({payload_len} bytes); " f"max is {protocol.MAX_PAYLOAD_SIZE}")
        if not 0 <= command_id <= protocol.UINT16_MAX:
            raise ValueError(f"Command id {command_id} outside 16-bit range")

        # Build the header and payload using Construct
        container = {
            "version": protocol.PROTOCOL_VERSION,
            "payload_len": payload_len,
            "command_id": command_id,
            "payload": payload,
        }
        data_to_crc = HeaderPayloadStruct.build(container)

        # Calculate CRC over the header and payload, then mask it to the
        # exact number of bits declared by the protocol.
        # Calculate mask based on protocol size (usually 4 bytes -> 0xFFFFFFFF)
        crc_mask = (1 << (protocol.CRC_SIZE * 8)) - 1

        # Use binascii.crc32 directly (standard IEEE 802.3) and mask to 32-bit unsigned,
        # then apply the protocol size mask.
        crc = (crc32(data_to_crc) & protocol.CRC32_MASK) & crc_mask

        # Pack the CRC
        crc_packed = cast(Any, protocol.CRC_STRUCT).build(crc)

        # Construct the full raw frame
        return data_to_crc + crc_packed

    @staticmethod
    def parse(raw_frame_buffer: bytes) -> tuple[int, bytes]:
        """Parse a decoded frame and validate header, payload, and CRC."""
        # 1. Verify minimum size
        if len(raw_frame_buffer) < protocol.MIN_FRAME_SIZE:
            raise ValueError(
                "Incomplete frame: size " f"{len(raw_frame_buffer)} is less than minimum " f"{protocol.MIN_FRAME_SIZE}"
            )

        # 2. Extract header and validate version early (optional but safer)
        # Note: CRC check still covers the whole frame, but we can verify
        # structural integrity first if desired. SIL-2 prefers CRC first.

        # [REFACTORED FOR 100% COVERAGE]
        # We ensure CRC_COVERED_HEADER_SIZE check is reachable.
        if len(raw_frame_buffer) < protocol.CRC_COVERED_HEADER_SIZE + protocol.CRC_SIZE:
            raise ValueError("Incomplete header")

        # 3. Extract and verify CRC
        crc_start = len(raw_frame_buffer) - protocol.CRC_SIZE
        data_to_check = raw_frame_buffer[:crc_start]
        received_crc_packed = raw_frame_buffer[crc_start:]

        try:
            received_crc = cast(Any, protocol.CRC_STRUCT).parse(received_crc_packed)
        except Exception as exc:
            raise ValueError(f"Failed to parse CRC: {exc}") from exc

        calculated_crc = crc32(data_to_check) & protocol.CRC32_MASK

        if received_crc != calculated_crc:
            raise ValueError(f"CRC mismatch. Expected {calculated_crc:08X}, " f"got {received_crc:08X}")

        # 4. Parse Header + Payload using Construct
        # This implicitly validates payload length vs buffer size via Terminated
        try:
            container = HeaderPayloadStruct.parse(data_to_check)
        except Exception as exc:
            raise ValueError(f"Frame structure error: {exc}") from exc

        version = container.version
        command_id = container.command_id
        payload = container.payload

        if version != protocol.PROTOCOL_VERSION:
            raise ValueError("Invalid version. Expected " f"{protocol.PROTOCOL_VERSION}, got {version}")

        # [SIL-2] Semantic Validation: Reject invalid/reserved command IDs (e.g. 0x00)
        # This prevents "noise" frames (valid CRC but nonsense ID) from reaching the
        # dispatcher and flooding logs with "Link not synchronized" warnings.
        if command_id < protocol.STATUS_CODE_MIN:
            raise ValueError(f"Invalid command id {command_id} (reserved/below minimum " f"{protocol.STATUS_CODE_MIN})")

        return command_id, payload

    def to_bytes(self) -> bytes:
        """Serialize the instance using :meth:`build`."""

        return self.build(self.command_id, self.payload)

    @classmethod
    def from_bytes(cls, raw_frame_buffer: bytes) -> "Frame":
        """Parse *raw_frame_buffer* and create a :class:`Frame`."""

        command_id, payload = cls.parse(raw_frame_buffer)
        return cls(command_id=command_id, payload=payload)
