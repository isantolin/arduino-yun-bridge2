# yunrpc/frame.py
# Provides classes for building and parsing RPC frames.

import struct
from typing import Tuple

from . import crc as Crc
from . import protocol


class Frame:
    @staticmethod
    def build(command_id: int, payload: bytes = b"") -> bytes:
        """Build a raw frame (header + payload + CRC) for COBS encoding."""
        payload_len = len(payload)
        if payload_len > protocol.MAX_PAYLOAD_SIZE:
            raise ValueError(
                (
                    f"Payload too large ({payload_len} bytes); "
                    f"max is {protocol.MAX_PAYLOAD_SIZE}"
                )
            )
        if not 0 <= command_id <= 0xFFFF:
            raise ValueError(
                f"Command id {command_id} outside 16-bit range"
            )

        # Pack the header that will be part of the CRC calculation
        crc_covered_header = struct.pack(
            protocol.CRC_COVERED_HEADER_FORMAT,
            protocol.PROTOCOL_VERSION,
            payload_len,
            command_id,
        )

        # Calculate CRC over the header and payload
        data_to_crc = crc_covered_header + payload
        crc = Crc.crc16_ccitt(data_to_crc)

        # Pack the CRC
        crc_packed = struct.pack(
            protocol.CRC_FORMAT,
            crc,
        )

        # Construct the full raw frame
        return crc_covered_header + payload + crc_packed

    @staticmethod
    def parse(raw_frame_buffer: bytes) -> Tuple[int, bytes]:
        """Parse a decoded frame and validate header, payload, and CRC."""
        # 1. Verify minimum size
        if len(raw_frame_buffer) < protocol.MIN_FRAME_SIZE:
            raise ValueError(
                (
                    "Incomplete frame: size "
                    f"{len(raw_frame_buffer)} is less than minimum "
                    f"{protocol.MIN_FRAME_SIZE}"
                )
            )

        # 2. Extract and verify CRC
        crc_start = len(raw_frame_buffer) - protocol.CRC_SIZE
        data_to_check = raw_frame_buffer[:crc_start]
        received_crc_packed = raw_frame_buffer[crc_start:]
        (received_crc,) = struct.unpack(
            protocol.CRC_FORMAT,
            received_crc_packed,
        )

        calculated_crc = Crc.crc16_ccitt(data_to_check)

        if received_crc != calculated_crc:
            raise ValueError(
                (
                    f"CRC mismatch. Expected {calculated_crc:04X}, "
                    f"got {received_crc:04X}"
                )
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
                (
                    "Invalid version. Expected "
                    f"{protocol.PROTOCOL_VERSION}, got {version}"
                )
            )

        # 4. Validate payload length against actual data length
        actual_payload_len = (
            len(data_to_check) - protocol.CRC_COVERED_HEADER_SIZE
        )
        if payload_len != actual_payload_len:
            raise ValueError(
                (
                    "Payload length mismatch. Header says "
                    f"{payload_len}, but got {actual_payload_len}"
                )
            )

        # 5. Extract payload
        payload = data_to_check[protocol.CRC_COVERED_HEADER_SIZE:]

        return command_id, payload
