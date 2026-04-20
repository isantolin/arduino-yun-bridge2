"""Command line tool to debug and generate MCU Bridge frames."""

from __future__ import annotations

import binascii
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated

import serial
import typer

# [SIL-2] Use direct library functions for framing
from cobs import cobs
import construct
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import Frame
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
        return (
            f"--- Frame Debug Snapshot ---\n"
            f"Command: {self.command_name} (0x{self.command_id:02X})\n"
            f"Payload Length: {self.payload_length} bytes\n"
            f"CRC32: 0x{self.crc:08X}\n"
            f"Raw Frame Size: {self.raw_length} bytes\n"
            f"COBS Encoded Size: {self.cobs_length} bytes\n"
            f"Total Serial Bytes: {self.expected_serial_bytes} (inc delimiter)\n"
            f"Raw Hex: [{self.raw_frame_hex}]\n"
            f"Encoded Hex: [{self.encoded_hex}]"
        )


def _resolve_command(cmd_str: str) -> int:
    if not cmd_str:
        raise ValueError("command may not be empty")
    try:
        if cmd_str.upper().startswith("0X"):
            return int(cmd_str, 16)
        # Try Command enum
        return int(getattr(protocol.Command, cmd_str.upper()))
    except (AttributeError, ValueError):
        try:
            # Try Status enum
            return int(getattr(protocol.Status, cmd_str.upper()))
        except (AttributeError, ValueError):
            try:
                return int(cmd_str)
            except ValueError:
                raise ValueError(f"Unknown command identifier: {cmd_str}")


def _parse_payload(payload_str: str | None) -> bytes:
    if not payload_str:
        return b""
    try:
        # Clean spacing/brackets if any
        clean = payload_str.replace(" ", "").replace("[", "").replace("]", "")
        if clean.upper().startswith("0X"):
            clean = clean[2:]
        return binascii.unhexlify(clean)
    except binascii.Error as exc:
        raise ValueError(f"Invalid hex payload: {exc}")


def _name_for_command(command_id: int) -> str:
    for enum_cls in (protocol.Command, protocol.Status):
        try:
            return enum_cls(command_id).name
        except ValueError:
            continue
    return f"UNKNOWN(0x{command_id:02X})"


def _hex_with_spacing(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def build_snapshot(command_id: int, payload: bytes) -> FrameDebugSnapshot:
    # Use sequence_id=0 for debug snapshots
    frame_obj = Frame(command_id=command_id, sequence_id=0, payload=payload)
    raw_frame = frame_obj.build()
    crc = int.from_bytes(raw_frame[-protocol.CRC_SIZE :], "big")
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
    except serial.SerialException as exc:
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
    return Frame.parse(cobs.decode(encoded_packet))


def _print_response(frame: Frame) -> None:
    sys.stdout.write(
        f"[FrameDebug] --- MCU Response ---\n"
        f"cmd_id=0x{int(frame.command_id):02X}\n"
        f"seq_id={frame.sequence_id}\n"
        f"payload_len={len(frame.payload)}\n"
    )


def _iter_counts(count: int) -> Iterable[int]:
    if count == 0:
        iteration = 0
        while True:
            yield iteration
            iteration += 1
    else:
        yield from range(count)


app = typer.Typer(add_completion=False, help="Inspect and optionally send MCU Bridge RPC frames.")


@app.command()
def main_cmd(
    command: Annotated[
        str, typer.Option("--command", "-c", help="Command or Status name/value")
    ] = "CMD_GET_FREE_MEMORY",
    payload: Annotated[str | None, typer.Option("--payload", "-p", help="Payload in hex format")] = None,
    port: Annotated[str | None, typer.Option(help="Serial port device path")] = None,
    baud: Annotated[int, typer.Option(help="Serial baud rate")] = DEFAULT_BAUDRATE,
    interval: Annotated[float, typer.Option(help="Interval between frames in seconds")] = 5.0,
    count: Annotated[int, typer.Option(help="Number of frames to send (0 for infinite)")] = 1,
    read_response: Annotated[bool, typer.Option(help="Wait for and print the next frame received")] = False,
    read_timeout: Annotated[float, typer.Option(help="Timeout for reading responses")] = 2.0,
):
    try:
        cmd_id = _resolve_command(command)
        payload_bytes = _parse_payload(payload)
    except ValueError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(2)

    ser = None
    if port:
        ser = _open_serial_device(port, baud, read_timeout)
        sys.stdout.write(f"[FrameDebug] Serial connected to {port} @ {baud} baud\n")

    try:
        for iteration in _iter_counts(count):
            snap = build_snapshot(cmd_id, payload_bytes)
            sys.stdout.write(snap.render() + "\n")
            if ser:
                written = _write_frame(ser, snap.encoded_packet)
                sys.stdout.write(f"[FrameDebug] wrote {written} bytes to serial port\n")
                if read_response:
                    resp = _read_frame(ser, read_timeout)
                    if not resp:
                        sys.stdout.write("[FrameDebug] No response before timeout\n")
                    else:
                        try:
                            _print_response(_decode_frame(resp))
                        except (ValueError, KeyError, TypeError, construct.ConstructError) as e:
                            sys.stderr.write(f"[FrameDebug] Failed to decode: {e}\n")
            if count == 0 or iteration + 1 < count:
                time.sleep(interval)
    finally:
        if ser:
            ser.close()


def main(argv: list[str] | None = None):
    if argv:
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app, argv)
        if result.exit_code != 0:
            if result.exception:
                raise result.exception
            raise SystemExit(result.exit_code)
        return 0
    app()


if __name__ == "__main__":
    main()
