"""RPC frame building and parsing for Arduino-Linux serial communication.

This module implements the binary frame format used over the serial link
between the Linux daemon and the Arduino MCU.

[SIL-2 COMPLIANCE]
The frame format is designed for reliable communication:
- CRC32 integrity check on every frame (via Construct Checksum)
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

from __future__ import annotations

from binascii import crc32
from typing import Any, cast

import msgspec
from construct import Bytes, Checksum, ChecksumError, Int8ub, Int16ub, Int32ub, RawCopy, Struct, this

from . import protocol


def _crc32_hash(data: bytes) -> int:
    """Compute CRC32 (IEEE 802.3) masked to 32-bit unsigned."""
    return crc32(data) & protocol.CRC32_MASK


# Frame structure with integrated CRC validation via Construct Checksum.
# RawCopy captures the header+payload bytes for CRC computation.
# Checksum automatically validates on parse and computes on build.
FrameStruct: Any = Struct(
    cast(Any, "body")
    / RawCopy(
        Struct(
            cast(Any, "version") / Int8ub,
            cast(Any, "payload_len") / Int16ub,
            cast(Any, "command_id") / Int16ub,
            cast(Any, "payload") / Bytes(this.payload_len),
        )
    ),
    cast(Any, "crc") / Checksum(Int32ub, _crc32_hash, this.body.data),
)


class Frame(msgspec.Struct, frozen=True, kw_only=True):
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

        # Build frame using Construct with integrated CRC computation
        container = {
            "body": {
                "value": {
                    "version": protocol.PROTOCOL_VERSION,
                    "payload_len": payload_len,
                    "command_id": command_id,
                    "payload": payload,
                }
            },
        }
        return FrameStruct.build(container)

    @staticmethod
    def parse(raw_frame_buffer: bytes) -> tuple[int, bytes]:
        """Parse a decoded frame and validate header, payload, and CRC."""
        # 1. Verify minimum size
        if len(raw_frame_buffer) < protocol.MIN_FRAME_SIZE:
            raise ValueError(
                "Incomplete frame: size " f"{len(raw_frame_buffer)} is less than minimum " f"{protocol.MIN_FRAME_SIZE}"
            )

        # 2. Verify header completeness before parsing
        if len(raw_frame_buffer) < protocol.CRC_COVERED_HEADER_SIZE + protocol.CRC_SIZE:
            raise ValueError("Incomplete header")

        # 3. Parse with integrated CRC validation via Construct
        try:
            container = FrameStruct.parse(raw_frame_buffer)
        except ChecksumError as exc:
            raise ValueError(f"CRC mismatch: {exc}") from exc
        except Exception as exc:
            raise ValueError(f"Frame structure error: {exc}") from exc

        # Extract fields from RawCopy container
        body = container.body.value
        version = body.version
        command_id = body.command_id
        payload = bytes(body.payload)

        # 4. Validate protocol version
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
