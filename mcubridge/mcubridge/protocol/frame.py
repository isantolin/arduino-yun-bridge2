"""RPC frame building and parsing for Arduino-Linux serial communication.

This module implements the binary frame format used over the serial link
between the Linux daemon and the Arduino MCU.

[SIL-2 COMPLIANCE]
- Zero manual orchestration logic.
- Direct Protobuf library usage (no wrappers).
- Explicit CRC32 validation.
"""

from __future__ import annotations

import asyncio
from binascii import crc32
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from google.protobuf.message import DecodeError, Message

from mcubridge.protocol import mcubridge_pb2 as pb

from . import protocol, is_system_command

_NONCE_SIZE: Final = protocol.AEAD_NONCE_SIZE
_TAG_SIZE: Final = protocol.AEAD_TAG_SIZE
_CRC_SIZE: Final = protocol.CRC_SIZE

_transient_payloads: dict[int, bytes] = {}


def _get_envelope_field_name_for_message(msg: Message) -> str | None:
    _MAP = {
        "VersionResponse": "version_resp",
        "FreeMemoryResponse": "free_memory_resp",
        "Capabilities": "capabilities",
        "PinMode": "pin_mode",
        "DigitalWrite": "digital_write",
        "AnalogWrite": "analog_write",
        "PinRead": "pin_read",
        "DigitalReadResponse": "digital_read_resp",
        "AnalogReadResponse": "analog_read_resp",
        "ConsoleWrite": "console_write",
        "DatastorePut": "datastore_put",
        "DatastoreGet": "datastore_get",
        "DatastoreGetResponse": "datastore_get_resp",
        "MailboxPush": "mailbox_push",
        "MailboxProcessed": "mailbox_processed",
        "MailboxAvailableResponse": "mailbox_available_resp",
        "MailboxReadResponse": "mailbox_read_resp",
        "FileWrite": "file_write",
        "FileRead": "file_read",
        "FileRemove": "file_remove",
        "FileReadResponse": "file_read_resp",
        "ProcessRunAsync": "process_run_async",
        "ProcessRunAsyncResponse": "process_run_async_resp",
        "ProcessPoll": "process_poll",
        "ProcessPollResponse": "process_poll_resp",
        "ProcessKill": "process_kill",
        "GenericResponse": "generic_resp",
        "AckPacket": "ack_packet",
        "HandshakeConfig": "handshake_config",
        "SetBaudratePacket": "set_baudrate_packet",
        "LinkSync": "link_sync",
        "EnterBootloader": "enter_bootloader",
        "SpiTransfer": "spi_transfer",
        "SpiTransferResponse": "spi_transfer_resp",
        "SpiConfig": "spi_config",
    }
    return _MAP.get(msg.__class__.__name__)


def build_frame(
    command_id: int,
    sequence_id: int,
    payload: bytes | Message = b"",
    nonce: bytes | None = None,
    tag: bytes | None = None,
    session_key: bytes | None = None,
) -> bytes:
    """Builds a binary frame using a Protobuf envelope directly. [SIL-2]"""
    if not (0 <= command_id <= protocol.UINT16_MAX):
        raise ValueError(f"Invalid command ID: {command_id}")

    is_excluded = is_system_command(command_id)

    # Initialize RpcEnvelope directly
    envelope = pb.RpcEnvelope(
        version=protocol.PROTOCOL_VERSION,
        command_id=command_id,
        sequence_id=sequence_id,
        nonce=nonce or (b"\x00" * _NONCE_SIZE),
        tag=tag or (b"\x00" * _TAG_SIZE),
    )

    payload_bytes = payload.SerializeToString() if isinstance(payload, Message) else payload
    if len(payload_bytes) > protocol.MAX_PAYLOAD_SIZE:
        raise ValueError(f"Payload size {len(payload_bytes)} exceeds maximum {protocol.MAX_PAYLOAD_SIZE}")

    # AEAD Encryption (if session key provided)
    do_encrypt = session_key and not is_excluded
    if do_encrypt:
        assert session_key is not None
        # Optimization: Use Protobuf envelope itself as AAD by only including header fields.
        aad = pb.RpcEnvelope(
            version=envelope.version, command_id=envelope.command_id, sequence_id=envelope.sequence_id
        ).SerializeToString()

        full_ct = ChaCha20Poly1305(session_key).encrypt(envelope.nonce, payload_bytes, aad)
        envelope.encrypted_payload, envelope.tag = full_ct[:-16], full_ct[-16:]
    else:
        # Unencrypted! Single-pass serialization.
        if isinstance(payload, Message):
            field_name = _get_envelope_field_name_for_message(payload)
            if field_name:
                setattr(envelope, field_name, payload)
            else:
                envelope.encrypted_payload = payload_bytes
        else:
            envelope.encrypted_payload = payload_bytes

    body = envelope.SerializeToString()
    return body + (crc32(body) & protocol.CRC32_MASK).to_bytes(4, "little")


def parse_frame(raw_frame_buffer: bytes | bytearray | memoryview, session_key: bytes | None = None) -> pb.RpcEnvelope:
    """Parses binary buffer directly into a Protobuf envelope. [SIL-2]"""
    buf = bytes(raw_frame_buffer)
    if len(buf) < _CRC_SIZE:
        raise ValueError("Incomplete frame: too short")

    body, crc_bytes = buf[:-_CRC_SIZE], buf[-_CRC_SIZE:]
    if (crc32(body) & protocol.CRC32_MASK) != int.from_bytes(crc_bytes, "little"):
        raise ValueError("CRC mismatch")

    envelope = pb.RpcEnvelope()
    try:
        envelope.ParseFromString(body)
    except DecodeError as e:
        raise ValueError(f"Failed to parse Protobuf envelope: {e}") from e

    if envelope.version != protocol.PROTOCOL_VERSION:
        raise ValueError("Invalid protocol version")

    is_excluded = is_system_command(envelope.command_id)

    # AEAD Decryption
    if session_key and not is_excluded:
        # Optimization: Use Protobuf envelope itself as AAD by only including header fields.
        aad = pb.RpcEnvelope(
            version=envelope.version, command_id=envelope.command_id, sequence_id=envelope.sequence_id
        ).SerializeToString()

        try:
            assert session_key is not None
            decrypted = ChaCha20Poly1305(session_key).decrypt(
                envelope.nonce, envelope.encrypted_payload + envelope.tag, aad
            )
        except InvalidTag as exc:
            raise ValueError("AEAD decryption failed") from exc
    else:
        # Unencrypted! Extract from oneof field and serialize/populate payload for compatibility
        active_field = envelope.WhichOneof("payload_type")
        if active_field == "encrypted_payload":
            decrypted = envelope.encrypted_payload
        elif active_field:
            decrypted = getattr(envelope, active_field).SerializeToString()
        else:
            decrypted = b""

    _transient_payloads[id(envelope)] = decrypted
    if len(_transient_payloads) > 1000:
        _transient_payloads.clear()

    try:
        loop = asyncio.get_running_loop()
        loop.call_soon(lambda env_id=id(envelope): _transient_payloads.pop(env_id, None))
    except RuntimeError:
        pass

    return envelope


def get_payload(envelope: pb.RpcEnvelope) -> bytes:
    """Extracts the transient/decrypted payload from a parsed envelope."""
    return _transient_payloads.get(id(envelope), b"")
