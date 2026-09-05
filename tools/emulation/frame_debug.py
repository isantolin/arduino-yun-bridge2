"""Command line tool to debug and generate MCU Bridge frames."""

from __future__ import annotations

import binascii
import sys
from collections.abc import Iterable
from dataclasses import dataclass

import typer
from typing import Annotated

import serialx
from google.protobuf.message import Message as ProtobufMessage

# [SIL-2] Use direct library functions for framing
from cobs import cobs
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import build_frame, parse_frame, DecodedFrame
from mcubridge.protocol.protocol import DEFAULT_BAUDRATE, FRAME_DELIMITER


@dataclass(frozen=True)
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
        """Renders the snapshot as a formatted string for display."""
        return (
            f"Command: {self.command_name} (0x{self.command_id:02X})\n"
            f"Payload Length: {self.payload_length} bytes\n"
            f"Raw Length: {self.raw_length} bytes\n"
            f"COBS Length: {self.cobs_length} bytes\n"
            f"CRC32: 0x{self.crc:08X}\n"
            f"Raw Frame (hex): {self.raw_frame_hex}\n"
            f"Encoded Frame (hex): {self.encoded_hex}\n"
            f"Total Wire Bytes: {self.expected_serial_bytes} bytes"
        )


def name_for_command(command_id: int) -> str:
    for enum_cls in (protocol.Command, protocol.Status):
        try:
            return enum_cls(command_id).name
        except ValueError:
            continue
    return f"UNKNOWN(0x{command_id:02X})"


def resolve_command(cmd_str: str) -> int:
    """Resolves a command string (hex, int, or name) to a command ID."""
    if not cmd_str:
        raise ValueError("command may not be empty")

    cmd_str = cmd_str.strip()
    if cmd_str.lower().startswith("0x"):
        return int(cmd_str, 16)
    if cmd_str.isdigit():
        return int(cmd_str)

    # Resolve by name
    for enum_cls in (protocol.Command, protocol.Status):
        for entry in enum_cls:
            if entry.name.upper() == cmd_str.upper():
                return entry.value

    raise ValueError(f"Unknown command: {cmd_str}")


def parse_payload(payload_str: str | None) -> bytes:
    """Parses a payload hex string into bytes."""
    if not payload_str:
        return b""

    # Remove 0x prefix and spaces
    clean_hex = payload_str.replace("0x", "").replace(" ", "")
    try:
        return binascii.unhexlify(clean_hex)
    except binascii.Error as e:
        if "Odd-length" in str(e):
            raise ValueError("Odd-length string") from e
        raise ValueError("Invalid hex payload") from e


def _hex_with_spacing(data: bytes) -> str:
    return data.hex(" ").upper()


def build_snapshot(command_id: int, payload: bytes | ProtobufMessage) -> FrameDebugSnapshot:
    # Use sequence_id=0 for debug snapshots
    raw_frame = build_frame(command_id=command_id, sequence_id=0, payload=payload)
    # CRC is at the end of the frame (little-endian)
    crc = int.from_bytes(raw_frame[-protocol.CRC_SIZE :], "little")
    encoded_body = cobs.encode(raw_frame)
    encoded_packet = encoded_body + FRAME_DELIMITER

    if isinstance(payload, (bytes, bytearray, memoryview)):
        payload_length = len(payload)
    elif hasattr(payload, "SerializeToString"):
        payload_length = len(payload.SerializeToString())
    else:
        payload_length = 0

    return FrameDebugSnapshot(
        command_id=command_id,
        command_name=name_for_command(command_id),
        payload_length=payload_length,
        crc=crc,
        raw_length=len(raw_frame),
        cobs_length=len(encoded_body),
        expected_serial_bytes=len(encoded_packet),
        encoded_packet=encoded_packet,
        raw_frame_hex=_hex_with_spacing(raw_frame),
        encoded_hex=_hex_with_spacing(encoded_packet),
    )


def _decode_frame(encoded_packet: bytes) -> DecodedFrame:
    return parse_frame(cobs.decode(encoded_packet))


def _print_response(decoded: DecodedFrame) -> None:
    envelope = decoded.envelope
    payload = decoded.payload
    if isinstance(payload, (bytes, bytearray, memoryview)):
        payload_len = len(payload)
    elif hasattr(payload, "SerializeToString"):
        payload_len = len(payload.SerializeToString())
    else:
        payload_len = 0

    sys.stdout.write(
        f"[FrameDebug] --- MCU Response ---\n"
        f"cmd_id=0x{envelope.command_id:02X}\n"
        f"seq_id={envelope.sequence_id}\n"
        f"payload_len={payload_len}\n"
    )


def _iter_counts(count: int) -> Iterable[int]:
    if count == 0:
        iteration = 0
        while True:
            yield iteration
            iteration += 1
    else:
        for i in range(count):
            yield i


async def run_debug_loop(
    port: str,
    baudrate: int,
    command_str: str,
    payload_str: str,
    interval: float,
    count: int,
) -> None:
    try:
        command_id = resolve_command(command_str)
        payload = parse_payload(payload_str)
    except ValueError as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)

    snapshot = build_snapshot(command_id, payload)
    sys.stdout.write(f"[FrameDebug] Initialized transport on {port} ({baudrate} bps)\n" f"{snapshot.render()}\n\n")

    try:
        reader, writer = await serialx.open_serial_connection(url=port, baudrate=baudrate)
        for i in _iter_counts(count):
            sys.stdout.write(f"[FrameDebug] [{i}] Sending frame...\n")
            writer.write(snapshot.encoded_packet)
            await writer.drain()

            try:
                # Wait for response with timeout
                async with asyncio.timeout(interval):
                    packet = await reader.readuntil(FRAME_DELIMITER)
                    if packet:
                        try:
                            decoded = _decode_frame(packet[:-1])
                            _print_response(decoded)
                        except (OSError, ValueError, RuntimeError, TypeError) as e:
                            sys.stderr.write(f"Error decoding response: {e}\n")
            except TimeoutError:
                sys.stdout.write("[FrameDebug] Timeout waiting for response\n")

            if count == 0 or i < count - 1:
                await asyncio.sleep(interval)

    except (OSError, ValueError, RuntimeError, TypeError) as e:
        sys.stderr.write(f"Serial Error: {e}\n")
        sys.exit(1)


async def run_generate_only(command_str: str, payload_str: str) -> None:
    try:
        command_id = resolve_command(command_str)
        payload = parse_payload(payload_str)
    except ValueError as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)

    snapshot = build_snapshot(command_id, payload)
    sys.stdout.write(f"{snapshot.render()}\n")


cli = typer.Typer(help="MCU Bridge Frame Debugger", add_completion=False)


@cli.command()
def main(
    command: Annotated[str, typer.Option("--command", "-c", help="Command ID (hex, int, or name)")] = "",
    port: Annotated[str | None, typer.Option("--port", "-p", help="Serial port device (e.g. /dev/ttyATH0)")] = None,
    baudrate: Annotated[int, typer.Option("--baudrate", "-b", help="Baudrate")] = DEFAULT_BAUDRATE,
    payload: Annotated[str, typer.Option("--payload", help="Payload hex string")] = "",
    interval: Annotated[float, typer.Option("--interval", help="Interval between frames")] = 1.0,
    count: Annotated[int, typer.Option("--count", help="Number of frames (0 for infinite)")] = 1,
    generate: Annotated[bool, typer.Option("--generate", help="Only generate and print frame")] = False,
    list_ports: Annotated[
        bool,
        typer.Option("--list-ports", "-l", help="List available serial ports and exit"),
    ] = False,
) -> None:
    if list_ports:
        ports = serialx.list_serial_ports()
        if not ports:
            sys.stdout.write("No serial ports detected.\n")
            return
        sys.stdout.write(f"Found {len(ports)} serial port(s):\n")
        for p in ports:
            hw_info: list[str] = []
            if p.vid is not None and p.pid is not None:
                hw_info.append(f"VID:PID={p.vid:04X}:{p.pid:04X}")
            if p.serial_number:
                hw_info.append(f"SER={p.serial_number}")
            hw_str = f" [{' '.join(hw_info)}]" if hw_info else ""
            desc_str = f" - {p.description}" if p.description else ""
            sys.stdout.write(f"  • {p.device}{desc_str}{hw_str}\n")
        return

    if not command:
        sys.stderr.write("Error: Missing option '--command' / '-c'.\n")
        sys.exit(1)

    if generate:
        asyncio.run(run_generate_only(command, payload))
    else:
        target_port = port
        if not target_port:
            detected = serialx.list_serial_ports()
            if len(detected) == 1:
                target_port = detected[0].device
                sys.stdout.write(f"[FrameDebug] Auto-detected serial port: {target_port}\n")
            else:
                sys.stderr.write(
                    "Error: Must specify --generate or --port <device>. Use --list-ports to see devices.\n"
                )
                sys.exit(1)

        asyncio.run(
            run_debug_loop(
                target_port,
                baudrate,
                command,
                payload,
                interval,
                count,
            )
        )


if __name__ == "__main__":
    import asyncio

    cli()
