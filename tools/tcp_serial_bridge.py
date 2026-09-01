#!/usr/bin/env python3
"""Dedicated high-reliability TCP <-> Serial Bridge for Arduino MCU (Zero-Latency)."""

import asyncio
import os
import signal
import sys
import termios

HOST = "0.0.0.0"
PORT = 9000
SERIAL_DEV = "/dev/ttyACM0"


def configure_serial(dev: str) -> int:
    fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    attrs[2] = termios.B115200 | termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[0] = 0  # raw input
    attrs[1] = 0  # raw output
    attrs[3] = 0  # raw line
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ser_fd: int):
    peer = writer.get_extra_info("peername")
    print(f"[BRIDGE] >>> New TCP connection from {peer}", flush=True)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def on_serial_readable() -> None:
        try:
            chunk = os.read(ser_fd, 1024)
            if chunk:
                writer.write(chunk)
        except (BlockingIOError, OSError):
            return

    loop.add_reader(ser_fd, on_serial_readable)

    async def tcp_reader_task() -> None:
        try:
            while not stop_event.is_set():
                data = await reader.read(1024)
                if not data:
                    print(f"[BRIDGE] TCP client {peer} closed connection.", flush=True)
                    break
                os.write(ser_fd, data)
        except (OSError, RuntimeError, asyncio.CancelledError) as e:
            print(f"[BRIDGE] TCP reader error: {e}", flush=True)
        finally:
            stop_event.set()

    t = asyncio.create_task(tcp_reader_task())
    await stop_event.wait()
    loop.remove_reader(ser_fd)
    t.cancel()
    try:
        writer.close()
        await writer.wait_closed()
    except OSError:
        pass
    print(f"[BRIDGE] <<< TCP client {peer} disconnected.", flush=True)


async def main():
    print(f"[BRIDGE] Opening {SERIAL_DEV} persistently...", flush=True)
    ser_fd = configure_serial(SERIAL_DEV)
    print(f"[BRIDGE] Serial port open (fd={ser_fd}). Starting TCP server on {HOST}:{PORT}...", flush=True)

    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, ser_fd),
        HOST,
        PORT,
        reuse_address=True,
    )

    async with server:
        print(f"[BRIDGE] ✅ Ready! Listening on {HOST}:{PORT}.", flush=True)
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
