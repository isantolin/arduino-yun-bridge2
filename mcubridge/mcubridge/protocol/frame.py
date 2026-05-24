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

from google.protobuf.message import DecodeError
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.security.security import aead_decrypt, aead_encrypt

from . import protocol
from .rle import rle_decode, rle_encode, should_compress

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
    """Represents an RPC frame for MCU-Linux communication using Protobuf Enveloping."""

    command_id: int | protocol.Command | protocol.Status
    sequence_id: int
    payload: bytes = b""
    nonce: bytes = b"\x00" * _NONCE_SIZE
    tag: bytes = b"\x00" * _TAG_SIZE

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
        return int(self.command_id) & ~protocol.CMD_FLAG_COMPRESSED & protocol.UINT16_MAX

    def build(self, session_key: bytes | None = None) -> bytes:
        """Builds the frame using a Protobuf envelope."""
        cmd_id = int(self.command_id)
        if not (0 <= cmd_id <= protocol.UINT16_MAX):
            raise ValueError(f"Invalid command ID: {cmd_id}")
        payload = self.payload

        if len(payload) > protocol.MAX_PAYLOAD_SIZE:
            raise ValueError(f"Payload size {len(payload)} exceeds maximum {protocol.MAX_PAYLOAD_SIZE}")

        raw_cmd = cmd_id & ~protocol.CMD_FLAG_COMPRESSED & protocol.UINT16_MAX
        is_excluded = (protocol.STATUS_CODE_MIN <= raw_cmd <= protocol.STATUS_CODE_MAX) or (
            protocol.SYSTEM_COMMAND_MIN <= raw_cmd <= protocol.SYSTEM_COMMAND_MAX
        )

        if payload and not is_excluded and should_compress(payload):
            compressed = rle_encode(payload)
            if len(compressed) < len(payload):
                payload = compressed
                cmd_id |= protocol.CMD_FLAG_COMPRESSED

        # Use Protobuf for the entire envelope instead of manual struct.pack
        envelope = pb.RpcEnvelope(
            version=protocol.PROTOCOL_VERSION,
            command_id=cmd_id,
            sequence_id=self.sequence_id,
            nonce=self.nonce,
            tag=self.tag,
            payload=payload,
        )

        if session_key and not is_excluded:
            # [SIL-2] Use Protobuf RpcEnvelope as AAD instead of manual struct.pack
            header_aad = pb.RpcEnvelope(
                version=protocol.PROTOCOL_VERSION,
                command_id=cmd_id,
                sequence_id=self.sequence_id,
            ).SerializeToString()
            inner_payload, tag = aead_encrypt(
                payload,
                session_key,
                self.nonce,
                header_aad,
            )
            envelope.payload = inner_payload
            envelope.tag = tag

        body = envelope.SerializeToString()
        crc = _frame_crc(body)
        return b"".join([body, CRC_STRUCT.pack(crc)])

    @classmethod
    def parse(cls, raw_frame_buffer: bytes | bytearray | memoryview, session_key: bytes | None = None) -> Frame:
        """Parses the frame using the Protobuf envelope."""
        buf = bytes(raw_frame_buffer)
        if len(buf) < _CRC_SIZE:
            raise ValueError("Incomplete or malformed frame: too short")

        body_len = len(buf) - _CRC_SIZE
        body = buf[:body_len]
        try:
            expected_crc = CRC_STRUCT.unpack(buf[body_len:])[0]
        except struct.error as e:
            raise ValueError(f"Malformed CRC field: {e}") from e

        if _frame_crc(body) != expected_crc:
            raise ValueError("CRC mismatch")

        envelope = pb.RpcEnvelope()
        try:
            envelope.ParseFromString(body)
        except DecodeError as e:
            raise ValueError(f"Failed to parse Protobuf envelope: {e}") from e
        if envelope.version != protocol.PROTOCOL_VERSION:
            raise ValueError("Invalid protocol version")

        cmd_id = envelope.command_id
        payload = envelope.payload
        nonce = envelope.nonce
        tag = envelope.tag

        raw_cmd = cmd_id & ~protocol.CMD_FLAG_COMPRESSED & protocol.UINT16_MAX
        is_excluded = (protocol.STATUS_CODE_MIN <= raw_cmd <= protocol.STATUS_CODE_MAX) or (
            protocol.SYSTEM_COMMAND_MIN <= raw_cmd <= protocol.SYSTEM_COMMAND_MAX
        )

        if session_key and not is_excluded:
            header_aad = pb.RpcEnvelope(
                version=envelope.version,
                command_id=cmd_id,
                sequence_id=envelope.sequence_id,
            ).SerializeToString()
            payload = aead_decrypt(payload, tag, session_key, nonce, header_aad)

        if cmd_id & protocol.CMD_FLAG_COMPRESSED:
            try:
                payload = rle_decode(payload)
                cmd_id &= ~protocol.CMD_FLAG_COMPRESSED
            except ValueError as e:
                raise ValueError(f"RLE decode failed: {e}") from e

        return cls(
            command_id=cmd_id,
            sequence_id=envelope.sequence_id,
            payload=payload,
            nonce=nonce,
            tag=tag,
        )
