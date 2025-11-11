"""Asyncio serial transport vendored for the Yun Bridge daemon.

This module is adapted from ``pyserial-asyncio`` 0.6 and includes
lightweight typing annotations plus compatibility fixes for modern Python
versions. The original project is available at
https://github.com/pyserial/pyserial-asyncio and is distributed under the
BSD-3-Clause license.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from typing import Any, Callable, Final, Tuple

import serial
from serial import SerialBase, SerialException

try:  # pragma: no cover - termios is unavailable on some platforms
    import termios  # type: ignore
except ImportError:  # pragma: no cover - fallback for platforms without termios
    termios = None

__all__ = [
    "SerialTransport",
    "create_serial_connection",
    "connection_for_serial",
    "open_serial_connection",
]

__version__ = "0.6-vendor"

if os.name == "nt":  # pragma: no cover - Windows is not part of the deployment target
    raise ImportError(
        "The vendored serial_asyncio module only supports POSIX platforms."
    )

ProtocolFactory = Callable[[], asyncio.Protocol]
StreamPair = Tuple[asyncio.StreamReader, asyncio.StreamWriter]

_DEFAULT_READ_CHUNK: Final[int] = 1024
_DEFAULT_HIGH_WATER: Final[int] = 64 * 1024
_DEFAULT_LOW_WATER: Final[int] = _DEFAULT_HIGH_WATER // 4
_DEFAULT_STREAM_LIMIT: Final[int] = 64 * 1024


class SerialTransport(asyncio.Transport):
    """Asyncio transport wrapping a :class:`serial.SerialBase` instance."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        protocol: asyncio.Protocol,
        serial_instance: SerialBase,
    ) -> None:
        super().__init__()
        self._loop: asyncio.AbstractEventLoop | None = loop
        self._protocol: asyncio.Protocol | None = protocol
        self._serial: SerialBase | None = serial_instance
        self._closing = False
        self._protocol_paused = False
        self._max_read_size = _DEFAULT_READ_CHUNK
        self._write_buffer: list[bytes] = []
        self._high_water = 0
        self._low_water = 0
        self._reader_registered = False
        self._writer_registered = False
        self._set_write_buffer_limits()

        # Configure serial port for non-blocking I/O required by asyncio.
        serial_instance.timeout = 0
        serial_instance.write_timeout = 0

        loop.call_soon(protocol.connection_made, self)
        loop.call_soon(self._ensure_reader)

    # ------------------------------------------------------------------
    # Introspection helpers
    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the event loop backing this transport."""

        return self._loop

    @property
    def serial(self) -> SerialBase | None:
        """Expose the underlying :mod:`pyserial` object."""

        return self._serial

    def get_extra_info(self, name: str, default: Any | None = None) -> Any | None:
        if name == "serial":
            return self._serial
        return default

    # ------------------------------------------------------------------
    # Base transport API
    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        if not self._closing:
            self._close()

    def abort(self) -> None:
        self._abort()

    # ------------------------------------------------------------------
    # Reading helpers
    def _read_ready(self) -> None:
        serial_obj = self._serial
        protocol = self._protocol
        if serial_obj is None or protocol is None:
            return
        try:
            data = serial_obj.read(self._max_read_size)
        except SerialException as exc:
            self._close(exc)
            return
        if data:
            protocol.data_received(data)

    def pause_reading(self) -> None:
        self._remove_reader()

    def resume_reading(self) -> None:
        self._ensure_reader()

    # ------------------------------------------------------------------
    # Writing helpers
    def write(self, data: bytes | bytearray | memoryview) -> None:
        if self._closing:
            return
        payload = bytes(data)
        if not self._write_buffer:
            self._write_buffer.append(payload)
            self._ensure_writer()
        else:
            self._write_buffer.append(payload)
        self._maybe_pause_protocol()

    def can_write_eof(self) -> bool:
        return False

    def write_eof(self) -> None:  # pragma: no cover - API mandated by base class
        raise NotImplementedError("Serial connections do not support EOF markers.")

    def flush(self) -> None:
        self._remove_writer()
        self._write_buffer.clear()
        self._maybe_resume_protocol()

    def get_write_buffer_size(self) -> int:
        return sum(len(chunk) for chunk in self._write_buffer)

    def set_write_buffer_limits(
        self,
        high: int | None = None,
        low: int | None = None,
    ) -> None:
        self._set_write_buffer_limits(high=high, low=low)
        self._maybe_pause_protocol()

    # ------------------------------------------------------------------
    # Internal flow control helpers
    def _maybe_pause_protocol(self) -> None:
        protocol = self._protocol
        if protocol is None:
            return
        if self.get_write_buffer_size() <= self._high_water:
            return
        if not self._protocol_paused:
            self._protocol_paused = True
            try:
                protocol.pause_writing()
            except Exception as exc:  # pragma: no cover - defensive
                self._dispatch_protocol_error("protocol.pause_writing() failed", exc)

    def _maybe_resume_protocol(self) -> None:
        protocol = self._protocol
        if protocol is None or not self._protocol_paused:
            return
        if self.get_write_buffer_size() > self._low_water:
            return
        self._protocol_paused = False
        try:
            protocol.resume_writing()
        except Exception as exc:  # pragma: no cover - defensive
            self._dispatch_protocol_error("protocol.resume_writing() failed", exc)

    def _write_ready(self) -> None:
        serial_obj = self._serial
        if serial_obj is None:
            return
        data = b"".join(self._write_buffer)
        if not data:
            return
        self._write_buffer.clear()
        try:
            written = serial_obj.write(data)
        except (BlockingIOError, InterruptedError):
            self._write_buffer.append(data)
        except SerialException as exc:
            self._fatal_error(exc, "Fatal write error on serial transport")
        else:
            if written == len(data):
                self._remove_writer()
                self._maybe_resume_protocol()
                if self._closing and self._flushed():
                    self._close()
            else:
                remaining = data[written:]
                self._write_buffer.append(remaining)
                self._maybe_resume_protocol()

    def _ensure_reader(self) -> None:
        if self._reader_registered or self._closing:
            return
        loop = self._loop
        serial_obj = self._serial
        if loop is None or serial_obj is None:
            return
        loop.add_reader(serial_obj.fileno(), self._read_ready)
        self._reader_registered = True

    def _remove_reader(self) -> None:
        if not self._reader_registered:
            return
        loop = self._loop
        serial_obj = self._serial
        if loop is None or serial_obj is None:
            self._reader_registered = False
            return
        loop.remove_reader(serial_obj.fileno())
        self._reader_registered = False

    def _ensure_writer(self) -> None:
        if self._writer_registered or self._closing:
            return
        loop = self._loop
        serial_obj = self._serial
        if loop is None or serial_obj is None:
            return
        loop.add_writer(serial_obj.fileno(), self._write_ready)
        self._writer_registered = True

    def _remove_writer(self) -> None:
        if not self._writer_registered:
            return
        loop = self._loop
        serial_obj = self._serial
        if loop is None or serial_obj is None:
            self._writer_registered = False
            return
        loop.remove_writer(serial_obj.fileno())
        self._writer_registered = False

    def _set_write_buffer_limits(
        self,
        *,
        high: int | None = None,
        low: int | None = None,
    ) -> None:
        if high is None:
            high = _DEFAULT_HIGH_WATER if low is None else 4 * low
        if low is None:
            low = high // 4
        if high < 0 or low < 0 or high < low:
            raise ValueError(
                "high (%r) must be >= low (%r) must be >= 0" % (high, low)
            )
        self._high_water = high
        self._low_water = low

    def _dispatch_protocol_error(self, message: str, exc: Exception) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.call_exception_handler(
            {
                "message": message,
                "exception": exc,
                "transport": self,
                "protocol": self._protocol,
            }
        )

    def _fatal_error(self, exc: Exception, message: str) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.call_exception_handler(
            {
                "message": message,
                "exception": exc,
                "transport": self,
                "protocol": self._protocol,
            }
        )
        self._abort(exc)

    def _flushed(self) -> bool:
        return not self._write_buffer

    def _close(self, exc: Exception | None = None) -> None:
        self._closing = True
        self._remove_reader()
        if self._flushed():
            self._remove_writer()
            loop = self._loop
            if loop is not None:
                loop.call_soon(self._call_connection_lost, exc)

    def _abort(self, exc: Exception | None = None) -> None:
        self._closing = True
        self._remove_reader()
        self._remove_writer()
        loop = self._loop
        if loop is not None:
            loop.call_soon(self._call_connection_lost, exc)

    def _call_connection_lost(self, exc: Exception | None) -> None:
        protocol = self._protocol
        serial_obj = self._serial
        if serial_obj is not None:
            with suppress(SerialException):
                if termios is not None:  # pragma: no branch - handled for mypy
                    with suppress(termios.error):
                        serial_obj.flush()
                else:
                    serial_obj.flush()
            serial_obj.close()
        if protocol is not None:
            protocol.connection_lost(exc)
        self._write_buffer.clear()
        self._protocol = None
        self._serial = None
        self._loop = None
        self._reader_registered = False
        self._writer_registered = False


async def create_serial_connection(
    *,
    loop: asyncio.AbstractEventLoop,
    protocol_factory: ProtocolFactory,
    **kwargs: Any,
) -> tuple[SerialTransport, asyncio.Protocol]:
    """Open a serial connection using :func:`serial.serial_for_url`."""

    serial_instance = serial.serial_for_url(**kwargs)
    protocol = protocol_factory()
    transport = SerialTransport(loop, protocol, serial_instance)
    return transport, protocol


async def connection_for_serial(
    loop: asyncio.AbstractEventLoop,
    protocol_factory: ProtocolFactory,
    serial_instance: SerialBase,
) -> tuple[SerialTransport, asyncio.Protocol]:
    protocol = protocol_factory()
    transport = SerialTransport(loop, protocol, serial_instance)
    return transport, protocol


async def open_serial_connection(
    *,
    loop: asyncio.AbstractEventLoop | None = None,
    limit: int | None = None,
    **kwargs: Any,
) -> StreamPair:
    """Return a :class:`StreamReader`/:class:`StreamWriter` pair for serial I/O."""

    if loop is None:
        loop = asyncio.get_running_loop()
    reader_limit = limit if limit is not None else _DEFAULT_STREAM_LIMIT
    reader = asyncio.StreamReader(limit=reader_limit)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await create_serial_connection(
        loop=loop,
        protocol_factory=lambda: protocol,
        **kwargs,
    )
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer
