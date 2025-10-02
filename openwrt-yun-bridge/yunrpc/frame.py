# yunrpc/frame.py
# Provides classes for building and parsing RPC frames.

import struct
from . import crc as Crc
from . import protocol

class Frame:
    @staticmethod
    def build(command_id, payload=b''):
        """Builds a frame ready to be sent."""
        payload_len = len(payload)
        
        # Pack the header that will be part of the CRC calculation
        crc_covered_header = struct.pack(protocol.CRC_COVERED_HEADER_FORMAT, protocol.PROTOCOL_VERSION, payload_len, command_id)
        
        # Calculate CRC over the header and payload
        data_to_crc = crc_covered_header + payload
        crc = Crc.crc16_ccitt(data_to_crc)
        
        # Pack the CRC
        crc_packed = struct.pack(protocol.CRC_FORMAT, crc)
        
        # Construct the full frame
        return bytes([protocol.START_BYTE]) + crc_covered_header + payload + crc_packed

    @staticmethod
    def parse(buffer):
        """
        Parses a complete frame buffer.
        Returns (command_id, payload) or raises ValueError for malformed frames.
        """
        if not buffer or buffer[0] != protocol.START_BYTE:
            raise ValueError("Invalid start byte")

        # The part of the buffer after the start byte contains the CRC'd data and the CRC itself
        frame_data = buffer[1:]
        
        # 1. Extract the CRC-covered header
        if len(frame_data) < protocol.CRC_COVERED_HEADER_SIZE:
            raise ValueError("Incomplete header")
        
        crc_covered_header = frame_data[:protocol.CRC_COVERED_HEADER_SIZE]
        version, payload_len, command_id = struct.unpack(protocol.CRC_COVERED_HEADER_FORMAT, crc_covered_header)

        if version != protocol.PROTOCOL_VERSION:
            raise ValueError(f"Invalid version. Expected {protocol.PROTOCOL_VERSION}, got {version}")

        # 2. Extract payload and CRC
        payload_start = protocol.CRC_COVERED_HEADER_SIZE
        payload_end = payload_start + payload_len
        
        if len(frame_data) < payload_end + protocol.CRC_SIZE:
            raise ValueError("Incomplete frame (payload or CRC missing)")
            
        payload = frame_data[payload_start:payload_end]
        
        received_crc_packed = frame_data[payload_end : payload_end + protocol.CRC_SIZE]
        received_crc, = struct.unpack(protocol.CRC_FORMAT, received_crc_packed)

        # 3. Verify CRC
        data_to_check = crc_covered_header + payload
        calculated_crc = Crc.crc16_ccitt(data_to_check)

        if received_crc != calculated_crc:
            raise ValueError(f"CRC mismatch. Expected {calculated_crc}, got {received_crc}")

        return command_id, payload
