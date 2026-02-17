"""RPC frame building and parsing for Arduino-Linux serial communication.

This module implements the binary frame format used over the serial link
between the Linux daemon and the Arduino MCU.

[SIL-2 COMPLIANCE]
The frame format is designed for reliable communication:
- CRC32 integrity check on every frame (calculated via standard binascii)
- Explicit length fields prevent buffer overruns
- Version field ensures protocol compatibility
- Big-endian byte order for cross-platform consistency

Frame Structure (on wire after COBS encoding):
    [Header (5 bytes)] [Payload (0-64 bytes)] [CRC32 (4 bytes)]

Header Format (big-endian):
    - version (1 byte): Protocol version, must match PROTOCOL_VERSION
    - payload_length (2 bytes): Number of payload bytes
    - command_id (2 bytes): Command or status code from protocol.py

The raw frame is then COBS-encoded and terminated with 0x00 delimiter
before transmission.
"""

from __future__ import annotations

from binascii import crc32

import msgspec
from construct import ConstructError
from mcubridge.protocol.structures import FRAME_STRUCT

from . import protocol


def _crc32_hash(data: bytes | bytearray | memoryview) -> int:
    """Compute CRC32 (IEEE 802.3) masked to 32-bit unsigned."""
    return crc32(data) & protocol.CRC32_MASK


class Frame(msgspec.Struct, frozen=True, kw_only=True):
    """Represents an RPC frame for MCU-Linux communication.

    This class provides both object-oriented and static methods for
    frame construction and parsing.

    Attributes:
        command_id: The RPC command or status code (16-bit).
        payload: The frame payload (0 to MAX_PAYLOAD_SIZE bytes).
    """

    command_id: int
    payload: bytes = b""

    @staticmethod
    def build(command_id: int, payload: bytes = b"") -> bytes:
        """Build a raw frame (header + payload + CRC) for COBS encoding.

        Implementation using 'construct' library for declarative serialization.
        """
        payload_len = len(payload)
        if payload_len > protocol.MAX_PAYLOAD_SIZE:
            raise ValueError(f"Payload too large ({payload_len} bytes); " f"max is {protocol.MAX_PAYLOAD_SIZE}")
        if not 0 <= command_id <= protocol.UINT16_MAX:
            raise ValueError(f"Command id {command_id} outside 16-bit range")

        # 1. Build the body (Header + Payload) using Construct
        # We need to construct the header+payload first to calculate CRC.
        # However, FRAME_STRUCT includes CRC at the end.
        # We can build the partial structure or build manually using sub-structs.
        # Using protocol.CRC_COVERED_HEADER_STRUCT directly is cleanest.

        header_data = protocol.CRC_COVERED_HEADER_STRUCT.build({
            "version": protocol.PROTOCOL_VERSION,
            "payload_len": payload_len,
            "command_id": command_id,
        })

        body = header_data + payload
        crc = _crc32_hash(body)

        # We could use FRAME_STRUCT.build(...) but that would require re-assembling
        # the dict. Since we already have the bytes, appending the CRC is efficient.
        # But to be strictly "construct prioritized", we use CRC_STRUCT for the CRC too.
        crc_bytes = protocol.CRC_STRUCT.build(crc)

        return body + crc_bytes

    @staticmethod
    def parse(raw_frame_buffer: bytes | bytearray | memoryview) -> tuple[int, bytes]:
        """Parse a decoded frame and validate header, payload, and CRC.

        Implementation using 'construct' library for declarative parsing.
        """
        # Ensure bytes for construct
        data_bytes = bytes(raw_frame_buffer)
        total_len = len(data_bytes)

        # 1. Verify minimum size (Header 5 + CRC 4 = 9 bytes)
        if total_len < protocol.MIN_FRAME_SIZE:
            raise ValueError(
                "Incomplete frame: size " f"{total_len} is less than minimum " f"{protocol.MIN_FRAME_SIZE}"
            )

        # 2. Parse using Construct (Handling structure and lengths)
        try:
            container = FRAME_STRUCT.parse(data_bytes)
        except ConstructError as e:
            raise ValueError(f"Frame parsing failed: {e}") from e

        # 3. Validate CRC
        # The body is everything EXCEPT the last 4 bytes (CRC)
        body = data_bytes[:-4]
        received_crc = container.crc
        calculated_crc = _crc32_hash(body)

        if received_crc != calculated_crc:
            raise ValueError(f"CRC mismatch: expected 0x{calculated_crc:08X}, got 0x{received_crc:08X}")

        # 4. Parse Header (already done by Construct, just validate semantics)
        version = container.header.version
        if version != protocol.PROTOCOL_VERSION:
            raise ValueError("Invalid version. Expected " f"{protocol.PROTOCOL_VERSION}, got {version}")

        # Payload Length Validation
        # Construct already ensured the payload matches payload_len in Bytes(this.header.payload_len).
        # We just need to check if the overall buffer had extra data, but Construct
        # usually consumes what it needs.
        # FRAME_STRUCT.parse consumes exactly header + payload + CRC.
        # If there are extra bytes at the end, 'parse' might ignore them unless we use GreedyBytes or similar check.
        # However, for our protocol, 'raw_frame_buffer' is a single decoded COBS frame, so it should match exactly.
        # Let's check strict sizing.
        expected_size = protocol.CRC_COVERED_HEADER_SIZE + container.header.payload_len + protocol.CRC_SIZE
        if total_len != expected_size:
            raise ValueError(
                f"Frame size mismatch: header says {container.header.payload_len} payload bytes, "
                f"but buffer has {len(body) - protocol.CRC_COVERED_HEADER_SIZE}"
            )

        if container.header.payload_len > protocol.MAX_PAYLOAD_SIZE:
            raise ValueError(f"Payload length {container.header.payload_len} exceeds max {protocol.MAX_PAYLOAD_SIZE}")

        # [SIL-2] Semantic Validation: Reject invalid/reserved command IDs
        command_id = container.header.command_id
        if command_id < protocol.STATUS_CODE_MIN:
            raise ValueError(f"Invalid command id {command_id} (reserved/below minimum " f"{protocol.STATUS_CODE_MIN})")

        return command_id, container.payload

    def to_bytes(self) -> bytes:
        """Serialize the instance using :meth:`build`."""
        return self.build(self.command_id, self.payload)

    @classmethod
    def from_bytes(cls, raw_frame_buffer: bytes | bytearray | memoryview) -> "Frame":
        """Parse *raw_frame_buffer* and create a :class:`Frame`."""
        command_id, payload = cls.parse(raw_frame_buffer)
        return cls(command_id=command_id, payload=payload)
