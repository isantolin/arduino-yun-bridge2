"""Frame inspection utility for MCU Bridge developers."""

from __future__ import annotations

import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated

import serial  # type: ignore
import typer
from cobs import cobs
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import Frame
from mcubridge.protocol.protocol import (
    DEFAULT_BAUDRATE,
    FRAME_DELIMITER,
    Command,
    Status,
)


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
    normalized = str(candidate).strip()
    try:
        return int(normalized, 0)
    except ValueError:
        pass
    normalized_upper = normalized.upper()
    try:
        return Command[normalized_upper].value
    except KeyError:
        pass
    try:
        return Status[normalized_upper].value
    except KeyError as exc:
        raise ValueError(f"Unknown command '{candidate}'.") from exc


def _parse_payload(hex_string: str | None) -> bytes:
    if not hex_string:
        return b""
    compact = "".join(str(hex_string).split())
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
    raw_frame = Frame(command_id=command_id, payload=payload).to_bytes()
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
    return Frame.from_bytes(cobs.decode(encoded_packet))


def _print_response(frame: Frame) -> None:
    sys.stdout.write(
        f"[FrameDebug] --- MCU Response ---\n"
        f"cmd_id=0x{frame.command_id:02X}\n"
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
                        except Exception as e:
                            sys.stderr.write(f"[FrameDebug] Failed to decode: {e}\n")
            if count == 0 or iteration + 1 < count:
                time.sleep(interval)
    finally:
        if ser:
            ser.close()


def main(argv=None):
    if argv:
        from typer.main import get_command
        from click.testing import CliRunner

        runner = CliRunner()
        # Ensure we pass the list correctly to the typer app
        result = runner.invoke(get_command(app), argv)
        if result.exit_code != 0:
            if result.exception:
                raise result.exception
            raise SystemExit(result.exit_code)
        return 0
    app()


if __name__ == "__main__":
    main()
