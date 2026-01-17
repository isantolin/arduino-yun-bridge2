"""Pure termios-based serial port implementation.

This module provides a pyserial-compatible interface using only Python's
built-in termios module. It eliminates the pyserial dependency for Linux/POSIX
systems, which is ideal for OpenWrt embedded deployments.

Only the subset of pyserial API used by McuBridge is implemented:
- open/close
- read/write
- in_waiting
- reset_input_buffer
- flush
- baudrate property
- fd property
- is_open property

This module is Linux/POSIX only. Windows is not supported.
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import termios
from typing import Any
from typing import Final

# Baudrate constants mapping
BAUDRATE_MAP: Final[dict[int, int]] = {
    50: termios.B50,
    75: termios.B75,
    110: termios.B110,
    134: termios.B134,
    150: termios.B150,
    200: termios.B200,
    300: termios.B300,
    600: termios.B600,
    1200: termios.B1200,
    1800: termios.B1800,
    2400: termios.B2400,
    4800: termios.B4800,
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
    230400: termios.B230400,
    460800: termios.B460800,
    500000: termios.B500000,
    576000: termios.B576000,
    921600: termios.B921600,
    1000000: termios.B1000000,
    1152000: termios.B1152000,
    1500000: termios.B1500000,
    2000000: termios.B2000000,
    2500000: termios.B2500000,
    3000000: termios.B3000000,
    3500000: termios.B3500000,
    4000000: termios.B4000000,
}


class SerialException(OSError):
    """Exception raised on serial port errors (pyserial compatible)."""
    pass


class TermiosSerial:
    """
    Pure termios-based serial port implementation.

    Provides a pyserial-compatible subset interface for Linux/POSIX.

    Usage:
        ser = TermiosSerial('/dev/ttyATH0', baudrate=115200)
        ser.open()
        data = ser.read(128)
        ser.write(b'hello')
        ser.close()
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float | None = None,
        exclusive: bool = False,
        do_not_open: bool = False,
    ) -> None:
        """
        Initialize serial port.

        Args:
            port: Device path (e.g., '/dev/ttyATH0')
            baudrate: Baud rate (default 115200)
            timeout: Read timeout in seconds (None = blocking, 0 = non-blocking)
            exclusive: Request exclusive access (TIOCEXCL)
            do_not_open: If True, don't open port in constructor
        """
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._exclusive = exclusive
        self._fd: int | None = None
        self._is_open = False
        self._original_attrs: list[Any] | None = None

        if not do_not_open:
            self.open()

    @property
    def port(self) -> str:
        """Return the port name."""
        return self._port

    @property
    def baudrate(self) -> int:
        """Return the current baudrate."""
        return self._baudrate

    @baudrate.setter
    def baudrate(self, value: int) -> None:
        """Set the baudrate."""
        self._baudrate = value
        if self._is_open and self._fd is not None:
            self._configure_port()

    @property
    def timeout(self) -> float | None:
        """Return the read timeout."""
        return self._timeout

    @timeout.setter
    def timeout(self, value: float | None) -> None:
        """Set the read timeout."""
        self._timeout = value

    @property
    def fd(self) -> int | None:
        """Return the file descriptor (pyserial compatibility)."""
        return self._fd

    @property
    def is_open(self) -> bool:
        """Return True if port is open."""
        return self._is_open

    @property
    def in_waiting(self) -> int:
        """Return number of bytes in input buffer."""
        if not self._is_open or self._fd is None:
            return 0
        try:
            import array
            buf = array.array('i', [0])
            fcntl.ioctl(self._fd, termios.FIONREAD, buf)
            return buf[0]
        except OSError:
            return 0

    @property
    def exclusive(self) -> bool:
        """Return exclusive mode setting."""
        return self._exclusive

    @exclusive.setter
    def exclusive(self, value: bool) -> None:
        """Set exclusive mode (must be set before open)."""
        self._exclusive = value

    def open(self) -> None:
        """Open the serial port."""
        if self._is_open:
            return

        try:
            # Open in read-write, non-controlling terminal mode
            self._fd = os.open(
                self._port,
                os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK
            )
        except OSError as e:
            raise SerialException(f"Could not open port {self._port}: {e}") from e

        try:
            # Request exclusive access if requested
            if self._exclusive:
                try:
                    fcntl.ioctl(self._fd, termios.TIOCEXCL)
                except (OSError, AttributeError):
                    pass  # TIOCEXCL not available on all platforms

            # Save original terminal attributes for restoration
            try:
                self._original_attrs = termios.tcgetattr(self._fd)
            except termios.error:
                self._original_attrs = None

            self._configure_port()
            self._is_open = True

        except (OSError, termios.error):  # pragma: no cover - cleanup guard
            os.close(self._fd)
            self._fd = None
            raise

    def _configure_port(self) -> None:
        """Configure the serial port with current settings."""
        if self._fd is None:
            return

        # Get termios baudrate constant
        if self._baudrate not in BAUDRATE_MAP:
            raise SerialException(f"Unsupported baudrate: {self._baudrate}")
        speed = BAUDRATE_MAP[self._baudrate]

        # Get current attributes
        try:
            attrs = termios.tcgetattr(self._fd)
        except termios.error as e:
            raise SerialException(f"Failed to get terminal attributes: {e}") from e

        # Input flags: disable all processing
        attrs[0] = 0  # iflag

        # Output flags: disable all processing
        attrs[1] = 0  # oflag

        # Control flags: 8N1, enable receiver, ignore modem control
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL

        # Local flags: raw mode (no echo, no signals, no canonical)
        attrs[3] = 0  # lflag

        # Control characters
        # VMIN: minimum bytes to read (0 for non-blocking with VTIME=0)
        # VTIME: timeout in deciseconds (0 for no timeout)
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0

        # Set input and output speed
        attrs[4] = speed  # ispeed
        attrs[5] = speed  # ospeed

        try:
            termios.tcsetattr(self._fd, termios.TCSANOW, attrs)
        except termios.error as e:
            raise SerialException(f"Failed to set terminal attributes: {e}") from e

        # Flush buffers
        try:
            termios.tcflush(self._fd, termios.TCIOFLUSH)
        except termios.error:
            pass

    def close(self) -> None:
        """Close the serial port."""
        if not self._is_open or self._fd is None:
            return

        try:
            # Restore original attributes if we saved them
            if self._original_attrs is not None:
                try:
                    termios.tcsetattr(self._fd, termios.TCSANOW, self._original_attrs)
                except termios.error:
                    pass

            os.close(self._fd)
        except OSError:
            pass
        finally:
            self._fd = None
            self._is_open = False
            self._original_attrs = None

    def read(self, size: int = 1) -> bytes:
        """
        Read up to 'size' bytes from the serial port.

        Returns immediately with available data (non-blocking by default).
        If timeout is set, waits up to timeout seconds for data.
        """
        if not self._is_open or self._fd is None:
            raise SerialException("Port not open")

        if size <= 0:
            return b''

        # Use select for timeout handling
        if self._timeout is not None and self._timeout > 0:
            ready, _, _ = select.select([self._fd], [], [], self._timeout)
            if not ready:
                return b''  # Timeout

        try:
            data = os.read(self._fd, size)
            return data
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return b''
            raise SerialException(f"Read error: {e}") from e

    def write(self, data: bytes | bytearray | memoryview) -> int:
        """
        Write data to the serial port.

        Returns the number of bytes written.
        """
        if not self._is_open or self._fd is None:
            raise SerialException("Port not open")

        if not data:
            return 0

        if isinstance(data, memoryview):
            data = bytes(data)
        elif isinstance(data, bytearray):
            data = bytes(data)

        try:
            return os.write(self._fd, data)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return 0
            raise SerialException(f"Write error: {e}") from e

    def flush(self) -> None:
        """Flush write buffers (wait for all data to be transmitted)."""
        if not self._is_open or self._fd is None:
            return

        try:
            termios.tcdrain(self._fd)
        except termios.error:
            pass

    def reset_input_buffer(self) -> None:
        """Clear input buffer, discarding all received data."""
        if not self._is_open or self._fd is None:
            return

        try:
            termios.tcflush(self._fd, termios.TCIFLUSH)
        except termios.error:
            pass

    def reset_output_buffer(self) -> None:
        """Clear output buffer, discarding all pending data."""
        if not self._is_open or self._fd is None:
            return

        try:
            termios.tcflush(self._fd, termios.TCOFLUSH)
        except termios.error:
            pass

    def fileno(self) -> int:
        """Return file descriptor (for use with select, etc.)."""
        if self._fd is None:
            raise SerialException("Port not open")
        return self._fd

    def __enter__(self) -> "TermiosSerial":
        """Context manager entry."""
        if not self._is_open:
            self.open()
        return self

    def __exit__(self, exc_type: type | None, exc_val: Exception | None, exc_tb: object) -> None:
        """Context manager exit."""
        self.close()

    def __del__(self) -> None:
        """Destructor - ensure port is closed."""
        self.close()


def serial_for_url(
    url: str,
    baudrate: int = 115200,
    **kwargs: object,
) -> TermiosSerial:
    """
    Create a TermiosSerial instance from a URL/path.

    This is a pyserial-compatible factory function.
    Only file:// and raw device paths are supported.

    Args:
        url: Device path or file:// URL
        baudrate: Baud rate
        **kwargs: Additional arguments (exclusive, do_not_open, timeout)

    Returns:
        TermiosSerial instance
    """
    # Strip file:// prefix if present
    if url.startswith("file://"):
        url = url[7:]

    # Extract supported kwargs
    exclusive = bool(kwargs.get("exclusive", False))
    do_not_open = bool(kwargs.get("do_not_open", False))
    timeout = kwargs.get("timeout")
    if timeout is not None:
        timeout = float(timeout)  # type: ignore[arg-type]

    return TermiosSerial(
        port=url,
        baudrate=baudrate,
        timeout=timeout,
        exclusive=exclusive,
        do_not_open=do_not_open,
    )


__all__ = [
    "TermiosSerial",
    "SerialException",
    "serial_for_url",
    "BAUDRATE_MAP",
]
