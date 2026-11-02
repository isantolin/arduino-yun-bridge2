"""Command line tool to debug and generate MCU Bridge Pigweed HDLC frames."""

from __future__ import annotations
from mcubridge.protocol.protocol import DEFAULT_BAUDRATE, FRAME_DELIMITER
from mcubridge.protocol import protocol
from google.protobuf.message import Message as ProtobufMessage

import asyncio
import binascii
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated
import serial
import serial.tools.list_ports
from serial.urlhandler import protocol_socket
import typer
from pw_hdlc.decode import FrameDecoder
from pw_rpc.internal.packet_pb2 import RpcPacket

from mcubridge.transport.serial import resolve_serial_url

sys.modules.setdefault("serial.urlhandler.protocol_wifi", protocol_socket)
sys.modules.setdefault("serial.urlhandler.protocol_tcp", protocol_socket)


@dataclass(frozen=True)
class FrameDebugSnapshot:
    command_id: int
    command_name: str
    payload_length: int
    hdlc_length: int
    encoded_packet: bytes
    encoded_hex: str

    def render(self) -> str:
        """Renders the snapshot as a formatted string for display."""
        return (
            f"Command: {self.command_name} (0x{self.command_id:02X})\n"
            f"Payload Length: {self.payload_length} bytes\n"
            f"HDLC Length: {self.hdlc_length} bytes\n"
            f"Encoded Frame (hex): {self.encoded_hex}\n"
            f"Total Wire Bytes: {len(self.encoded_packet)} bytes"
        )


_ENUM_BY_NAME: dict[str, int] = {
    entry.name.upper(): entry.value for enum_cls in (protocol.Command, protocol.Status) for entry in enum_cls
}
_ENUM_BY_VALUE: dict[int, str] = {
    entry.value: entry.name for enum_cls in (protocol.Command, protocol.Status) for entry in enum_cls
}


def name_for_command(command_id: int) -> str:
    return _ENUM_BY_VALUE.get(command_id, f"UNKNOWN(0x{command_id:02X})")


def resolve_command(cmd_str: str) -> int:
    """Resolves a command string (hex, int, or name) to a command ID."""
    if not cmd_str:
        raise ValueError("command may not be empty")

    clean = cmd_str.strip()
    if clean.lower().startswith("0x"):
        return int(clean, 16)
    if clean.isdigit():
        return int(clean)

    cmd_val = _ENUM_BY_NAME.get(clean.upper())
    if cmd_val is not None:
        return cmd_val

    raise ValueError(f"Unknown command: {cmd_str}")


def parse_payload(payload_str: str | None) -> bytes:
    """Parses a payload hex string into bytes."""
    if not payload_str:
        return b""

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
    encoded_packet = protocol.encode_hdlc_rpc(command_id, payload, call_id=0)

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
        hdlc_length=len(encoded_packet),
        encoded_packet=encoded_packet,
        encoded_hex=_hex_with_spacing(encoded_packet),
    )


def _decode_frame(encoded_packet: bytes) -> tuple[int, int, bytes]:
    decoder = FrameDecoder()
    for frame in decoder.process(encoded_packet):
        if frame.ok() and frame.address == protocol.HDLC_ADDRESS_RPC:
            pkt = RpcPacket()
            pkt.ParseFromString(frame.data)
            op = protocol.get_operation((pkt.service_id, pkt.method_id))
            cmd_id = op.command_id if op and op.command_id is not None else pkt.method_id
            return cmd_id, pkt.call_id, pkt.payload
    raise ValueError("No valid HDLC RPC frame found")


def _print_response(cmd_id: int, call_id: int, payload: bytes) -> None:
    sys.stdout.write(
        f"[FrameDebug] --- MCU Response ---\ncmd_id=0x{cmd_id:02X}\ncall_id={call_id}\npayload_len={len(payload)}\n"
    )


def _iter_counts(count: int) -> Iterable[int]:
    if count == 0:
        iteration = 0
        while True:
            yield iteration
            iteration += 1
    else:
        yield from range(count)


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
    sys.stdout.write(f"[FrameDebug] Initialized transport on {port} ({baudrate} bps)\n{snapshot.render()}\n\n")

    try:
        url = resolve_serial_url(port)
        ser = await asyncio.to_thread(
            serial.serial_for_url,
            url,
            baudrate=baudrate,
            timeout=interval,
            write_timeout=2.0,
        )
        try:
            for i in _iter_counts(count):
                sys.stdout.write(f"[FrameDebug] [{i}] Sending frame...\n")
                await asyncio.to_thread(ser.write, snapshot.encoded_packet)
                await asyncio.to_thread(ser.flush)

                try:
                    packet = await asyncio.to_thread(ser.read_until, FRAME_DELIMITER)
                    if packet:
                        try:
                            cmd_id, call_id, resp_payload = _decode_frame(packet)
                            _print_response(cmd_id, call_id, resp_payload)
                        except (OSError, ValueError, RuntimeError, TypeError) as e:
                            sys.stderr.write(f"Error decoding response: {e}\n")
                    else:
                        sys.stdout.write("[FrameDebug] Timeout waiting for response\n")
                except TimeoutError:
                    sys.stdout.write("[FrameDebug] Timeout waiting for response\n")

                if count == 0 or i < count - 1:
                    await asyncio.sleep(interval)
        finally:
            await asyncio.to_thread(ser.close)

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
def generate(
    command: Annotated[str, typer.Option("--command", "-c", help="Command name (e.g. CMD_DIGITAL_READ) or ID")],
    payload: Annotated[str, typer.Option("--payload", "-p", help="Payload in hex format (e.g. 0x010203)")] = "",
) -> None:
    """Generate and display frame debug information without sending."""
    import anyio

    anyio.run(run_generate_only, command, payload)


@cli.command()
def send(
    port: Annotated[str, typer.Option("--port", help="Serial port URL, device or TCP socket")] = "/dev/ttyATH0",
    baudrate: Annotated[int, typer.Option("--baudrate", help="Serial baudrate")] = DEFAULT_BAUDRATE,
    command: Annotated[
        str, typer.Option("--command", "-c", help="Command name (e.g. CMD_DIGITAL_READ) or ID")
    ] = "CMD_GET_VERSION",
    payload: Annotated[str, typer.Option("--payload", "-p", help="Payload in hex format (e.g. 0x010203)")] = "",
    interval: Annotated[float, typer.Option("--interval", "-i", help="Interval between frames in seconds")] = 1.0,
    count: Annotated[int, typer.Option("--count", "-n", help="Number of frames to send (0 = infinite)")] = 1,
) -> None:
    """Generate and transmit frames over the serial transport."""
    import anyio

    anyio.run(run_debug_loop, port, baudrate, command, payload, interval, count)


@cli.command()
def list_ports() -> None:
    """List available serial ports and exit."""
    ports = serial.tools.list_ports.comports()
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


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
