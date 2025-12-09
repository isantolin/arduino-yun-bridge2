"""Tests for serial noise handling in the daemon reader."""
from __future__ import annotations

import asyncio
from typing import cast

from cobs import cobs
from yunbridge.transport.serial import _process_serial_packet
from yunbridge.rpc.frame import Frame
from yunbridge.rpc.protocol import Command, Status
from yunbridge.services.runtime import BridgeService


class _StubService:
    def __init__(self) -> None:
        self.sent_frames: list[tuple[int, bytes]] = []
        self.handled_frames: list[tuple[int, bytes]] = []

    async def send_frame(self, command_id: int, payload: bytes) -> bool:
        self.sent_frames.append((command_id, bytes(payload)))
        return True

    async def handle_mcu_frame(self, command_id: int, payload: bytes) -> None:
        self.handled_frames.append((command_id, bytes(payload)))


def test_process_serial_packet_records_decode_error(runtime_state) -> None:
    stub = _StubService()
    service = cast(BridgeService, stub)

    asyncio.run(_process_serial_packet(b"\x00", service, runtime_state))

    assert runtime_state.serial_decode_errors == 1
    assert runtime_state.serial_crc_errors == 0
    assert stub.sent_frames
    assert stub.sent_frames[-1][0] == Status.MALFORMED.value
    assert not stub.handled_frames


def test_process_serial_packet_records_crc_error(runtime_state) -> None:
    stub = _StubService()
    service = cast(BridgeService, stub)
    frame = Frame(Command.CMD_MAILBOX_PUSH.value, b"\xAA\x55")
    raw_frame = frame.to_bytes()
    corrupted = bytearray(raw_frame)
    corrupted[-1] ^= 0xFF
    encoded = cobs.encode(bytes(corrupted))

    asyncio.run(_process_serial_packet(encoded, service, runtime_state))

    assert runtime_state.serial_crc_errors == 1
    assert runtime_state.serial_decode_errors == 0
    assert stub.sent_frames
    assert stub.sent_frames[-1][0] == Status.CRC_MISMATCH.value
    assert not stub.handled_frames


def test_process_serial_packet_forwards_valid_frames(runtime_state) -> None:
    stub = _StubService()
    service = cast(BridgeService, stub)

    frame = Frame(Command.CMD_CONSOLE_WRITE.value, b"hi")
    encoded = cobs.encode(frame.to_bytes())

    asyncio.run(_process_serial_packet(encoded, service, runtime_state))

    assert stub.handled_frames == [
        (Command.CMD_CONSOLE_WRITE.value, b"hi"),
    ]
    assert not stub.sent_frames


def test_process_serial_packet_rejects_non_binary_payload(runtime_state) -> None:
    stub = _StubService()
    service = cast(BridgeService, stub)

    asyncio.run(_process_serial_packet("not-bytes", service, runtime_state))

    assert runtime_state.serial_decode_errors == 1
    assert not stub.handled_frames
    assert stub.sent_frames
    assert stub.sent_frames[-1][0] == Status.MALFORMED.value
