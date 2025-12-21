"""Frame inspection utility for Yun Bridge developers.

This tool mirrors the Arduino-side FrameDebug example: it builds RPC
frames, prints their metadata, and optionally writes them to the MCU
serial port while decoding responses.  The daemon must be stopped before
running it because /dev/ttyATH0 cannot be shared.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from collections.abc import Iterable

import serial

from cobs import cobs
from yunbridge.const import (
    DEFAULT_SERIAL_BAUD,
    FRAME_DELIMITER,
)
from yunbridge.rpc import protocol as rpc_protocol
from yunbridge.rpc.frame import Frame
from yunbridge.rpc.protocol import Command, Status


@dataclass(slots=True)
class FrameDebugSnapshot:
    command_id: int
    command_name: str
    payload_length: int
    crc: int
    raw_length: int
    cobs_length: int
    expected_serial_bytes: int
    encoded_packet: bytes
    raw_frame_hex: str
    encoded_hex: str

    def render(self) -> str:
        return (
            "[FrameDebug] --- Snapshot ---\n"
            f"cmd_id=0x{self.command_id:02X} ({self.command_name})\n"
            f"payload_len={self.payload_length}\n"
            f"crc=0x{self.crc:08X}\n"
            f"raw_len={self.raw_length}\n"
            f"cobs_len={self.cobs_length}\n"
            f"expected_serial_bytes={self.expected_serial_bytes}\n"
            f"raw_frame={self.raw_frame_hex}\n"
            f"encoded={self.encoded_hex}"
        )


def _resolve_command(candidate: str) -> int:
    if not candidate:
        raise ValueError("command may not be empty")

    normalized = candidate.strip().upper()
    if normalized.startswith("0X"):
        normalized = normalized[2:]
    try:
        return int(normalized, 16)
    except ValueError:
        pass

    try:
        return Command[normalized].value
    except KeyError as exc:
        raise ValueError(
            f"Unknown command '{candidate}'. Use hex (e.g. 0x03) "
            "or a Command enum name."
        ) from exc


def _parse_payload(hex_string: str | None) -> bytes:
    if not hex_string:
        return b""
    compact = "".join(hex_string.split())
    if compact.startswith("0x") or compact.startswith("0X"):
        compact = compact[2:]
    if len(compact) % 2:
        raise ValueError("payload hex must contain an even number of digits")
    try:
        return bytes.fromhex(compact)
    except ValueError as exc:
        raise ValueError(f"Invalid payload hex '{hex_string}': {exc}") from exc


def _name_for_command(command_id: int) -> str:
    try:
        return Command(command_id).name
    except ValueError:
        try:
            return Status(command_id).name
        except ValueError:
            return f"UNKNOWN(0x{command_id:02X})"


def _hex_with_spacing(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def build_snapshot(command_id: int, payload: bytes) -> FrameDebugSnapshot:
    raw_frame = Frame(command_id, payload).to_bytes()
    crc = int.from_bytes(raw_frame[-rpc_protocol.CRC_SIZE :], "big")
    encoded_body = cobs.encode(raw_frame)
    encoded_packet = encoded_body + FRAME_DELIMITER
    return FrameDebugSnapshot(
        command_id=command_id,
        command_name=_name_for_command(command_id),
        payload_length=len(payload),
        crc=crc,
        raw_length=len(raw_frame),
        cobs_length=len(encoded_body),
        expected_serial_bytes=len(encoded_packet),
        encoded_packet=encoded_packet,
        raw_frame_hex=_hex_with_spacing(raw_frame),
        encoded_hex=_hex_with_spacing(encoded_packet),
    )


def _open_serial_device(port: str, baud: int, timeout: float) -> serial.Serial:
    try:
        return serial.Serial(port=port, baudrate=baud, timeout=timeout)
    except serial.SerialException as exc:  # pragma: no cover - hardware path
        raise SystemExit(f"Failed to open serial port {port}: {exc}") from exc


def _write_frame(device: serial.Serial, encoded_packet: bytes) -> int:
    written = device.write(encoded_packet)
    device.flush()
    return int(written) if written is not None else 0


def _read_frame(device: serial.Serial, timeout: float) -> bytes | None:
    buffer = bytearray()
    deadline = time.monotonic() + timeout if timeout > 0 else None
    while True:
        if deadline is not None and time.monotonic() > deadline:
            return None
        chunk = device.read(1)
        if not chunk:
            continue
        if chunk == FRAME_DELIMITER:
            if buffer:
                return bytes(buffer)
            continue
        buffer.extend(chunk)


def _decode_frame(encoded_packet: bytes) -> Frame:
    raw_frame = cobs.decode(encoded_packet)
    return Frame.from_bytes(raw_frame)


def _print_response(frame: Frame) -> None:
    payload_hex = frame.payload.hex()
    payload_preview = payload_hex[:64]
    if len(payload_hex) > 64:
        payload_preview += "…"
    print("[FrameDebug] --- MCU Response ---")
    command_name = _name_for_command(frame.command_id)
    print(f"cmd_id=0x{frame.command_id:02X} ({command_name})")
    print(f"payload_len={len(frame.payload)}")
    print(f"payload={payload_preview}")


def _positive_float(value: str) -> float:
    candidate = float(value)
    if candidate <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return candidate


def _non_negative_int(value: str) -> int:
    candidate = int(value)
    if candidate < 0:
        raise argparse.ArgumentTypeError("value must be >= 0")
    return candidate


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect and optionally send Yun Bridge RPC frames. "
            "Stop the yunbridge daemon before using --port."
        )
    )
    parser.add_argument(
        "--command",
        "-c",
        default="CMD_GET_FREE_MEMORY",
        help=(
            "Command to build. Accepts enum name (e.g. CMD_LINK_RESET) "
            "or hex literal such as 0x03."
        ),
    )
    parser.add_argument(
        "--payload",
        "-p",
        help="Optional payload as hex string (spaces allowed).",
    )
    parser.add_argument(
        "--port",
        help="Serial device to write frames to (omit to skip I/O).",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=DEFAULT_SERIAL_BAUD,
        help=f"Serial baud rate (default: {DEFAULT_SERIAL_BAUD}).",
    )
    parser.add_argument(
        "--interval",
        type=_positive_float,
        default=5.0,
        help="Seconds to wait between frames when count != 1 (default: 5).",
    )
    parser.add_argument(
        "--count",
        type=_non_negative_int,
        default=1,
        help=(
            "Number of frames to send. 0 means run indefinitely without "
            "delay between iterations."
        ),
    )
    parser.add_argument(
        "--read-response",
        action="store_true",
        help="After sending a frame, wait for one MCU response and decode it.",
    )
    parser.add_argument(
        "--read-timeout",
        type=_positive_float,
        default=2.0,
        help=(
            "Seconds to wait for a response when --read-response is set "
            "(default: 2)."
        ),
    )
    return parser


def _iter_counts(count: int) -> Iterable[int]:
    if count == 0:
        iteration = 0
        while True:
            yield iteration
            iteration += 1
    else:
        yield from range(count)


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        command_id = _resolve_command(args.command)
        payload = _parse_payload(args.payload)
    except ValueError as exc:
        parser.error(str(exc))
        return 2

    serial_device: serial.Serial | None = None
    if args.port:
        serial_device = _open_serial_device(
            args.port,
            args.baud,
            args.read_timeout,
        )
        print(f"[FrameDebug] Serial connected to {args.port} @ {args.baud} baud")

    try:
        for iteration in _iter_counts(args.count):
            snapshot = build_snapshot(command_id, payload)
            print(snapshot.render())
            if serial_device:
                written = _write_frame(serial_device, snapshot.encoded_packet)
                print(f"[FrameDebug] wrote {written} bytes to serial port")
                if args.read_response:
                    encoded_response = _read_frame(
                        serial_device, timeout=args.read_timeout
                    )
                    if not encoded_response:
                        print("[FrameDebug] No response before timeout")
                    else:
                        try:
                            response_frame = _decode_frame(encoded_response)
                        except Exception as exc:
                            print(
                                "[FrameDebug] Failed to decode MCU response: " f"{exc}"
                            )
                        else:
                            _print_response(response_frame)
            if args.count == 0 or iteration + 1 < args.count:
                time.sleep(args.interval)
    finally:
        if serial_device:
            serial_device.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
