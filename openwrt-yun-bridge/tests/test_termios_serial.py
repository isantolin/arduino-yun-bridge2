"""Tests for yunbridge.transport.termios_serial module."""

from __future__ import annotations

import os
import pty
import pytest

from yunbridge.transport.termios_serial import (
    TermiosSerial,
    SerialException,
    serial_for_url,
    BAUDRATE_MAP,
)


class TestTermiosSerialBasic:
    """Basic functionality tests using PTY."""

    def test_serial_exception_is_os_error(self) -> None:
        """Verify SerialException is a subclass of OSError."""
        exc = SerialException("test error")
        assert isinstance(exc, OSError)
        assert str(exc) == "test error"

    def test_baudrate_map_has_common_speeds(self) -> None:
        """Check BAUDRATE_MAP contains commonly used baudrates."""
        assert 9600 in BAUDRATE_MAP
        assert 115200 in BAUDRATE_MAP
        assert 230400 in BAUDRATE_MAP
        assert 460800 in BAUDRATE_MAP

    def test_open_nonexistent_port_raises(self) -> None:
        """Opening a non-existent port should raise SerialException."""
        with pytest.raises(SerialException):
            TermiosSerial("/dev/nonexistent_port_xyz", baudrate=115200)

    def test_serial_for_url_strips_file_prefix(self) -> None:
        """Verify serial_for_url strips file:// prefix."""
        with pytest.raises(SerialException):
            serial_for_url("file:///dev/nonexistent_port_xyz", baudrate=115200)

    def test_do_not_open_flag(self) -> None:
        """Verify do_not_open=True delays opening."""
        ser = TermiosSerial(
            "/dev/nonexistent_port_xyz",
            baudrate=115200,
            do_not_open=True,
        )
        assert not ser.is_open
        assert ser.fd is None
        assert ser.port == "/dev/nonexistent_port_xyz"
        assert ser.baudrate == 115200


class TestTermiosSerialWithPTY:
    """Tests using PTY for simulated serial port."""

    @pytest.fixture
    def pty_pair(self) -> tuple[int, str]:
        """Create a PTY pair for testing."""
        master_fd, slave_fd = pty.openpty()
        slave_name = os.ttyname(slave_fd)
        # Close slave side; we'll open it via TermiosSerial
        os.close(slave_fd)
        yield master_fd, slave_name
        os.close(master_fd)

    def test_open_and_close(self, pty_pair: tuple[int, str]) -> None:
        """Test opening and closing a PTY as serial port."""
        master_fd, slave_name = pty_pair

        ser = TermiosSerial(slave_name, baudrate=115200)
        assert ser.is_open
        assert ser.fd is not None
        assert ser.port == slave_name

        ser.close()
        assert not ser.is_open
        assert ser.fd is None

    def test_context_manager(self, pty_pair: tuple[int, str]) -> None:
        """Test context manager interface."""
        master_fd, slave_name = pty_pair

        with TermiosSerial(slave_name, baudrate=115200) as ser:
            assert ser.is_open
        assert not ser.is_open

    def test_write_and_read(self, pty_pair: tuple[int, str]) -> None:
        """Test basic write/read through PTY."""
        master_fd, slave_name = pty_pair

        with TermiosSerial(slave_name, baudrate=115200, timeout=1.0) as ser:
            # Write from TermiosSerial side
            test_data = b"hello"
            written = ser.write(test_data)
            assert written == len(test_data)
            ser.flush()

            # Read from master side
            received = os.read(master_fd, 128)
            assert received == test_data

    def test_read_from_master(self, pty_pair: tuple[int, str]) -> None:
        """Test reading data sent from master side."""
        master_fd, slave_name = pty_pair

        with TermiosSerial(slave_name, baudrate=115200, timeout=0.5) as ser:
            # Write from master side
            test_data = b"world"
            os.write(master_fd, test_data)

            # Read from TermiosSerial side
            received = ser.read(128)
            assert received == test_data

    def test_in_waiting(self, pty_pair: tuple[int, str]) -> None:
        """Test in_waiting property."""
        master_fd, slave_name = pty_pair

        with TermiosSerial(slave_name, baudrate=115200, timeout=0.5) as ser:
            # Initially nothing waiting
            assert ser.in_waiting >= 0  # Could be 0 or more

            # Write from master
            os.write(master_fd, b"abc")
            # Give time for data to arrive
            import time
            time.sleep(0.1)

            # Should have bytes waiting
            assert ser.in_waiting > 0

    def test_reset_input_buffer(self, pty_pair: tuple[int, str]) -> None:
        """Test reset_input_buffer clears input."""
        master_fd, slave_name = pty_pair

        with TermiosSerial(slave_name, baudrate=115200, timeout=0.1) as ser:
            # Write some data from master
            os.write(master_fd, b"junk")
            import time
            time.sleep(0.05)

            # Reset buffer
            ser.reset_input_buffer()

            # Write fresh data
            os.write(master_fd, b"fresh")
            time.sleep(0.05)

            # Should only get fresh data
            received = ser.read(128)
            assert b"fresh" in received

    def test_baudrate_setter(self, pty_pair: tuple[int, str]) -> None:
        """Test changing baudrate on open port."""
        master_fd, slave_name = pty_pair

        with TermiosSerial(slave_name, baudrate=9600) as ser:
            assert ser.baudrate == 9600

            ser.baudrate = 115200
            assert ser.baudrate == 115200

    def test_unsupported_baudrate_raises(self, pty_pair: tuple[int, str]) -> None:
        """Test that unsupported baudrate raises exception."""
        master_fd, slave_name = pty_pair

        # 12345 is neither in BAUDRATE_MAP nor a valid custom baudrate
        # Note: With BOTHER support, custom baudrates like 250000 are now valid
        with pytest.raises((SerialException, OSError)):
            TermiosSerial(slave_name, baudrate=12345)

    def test_custom_baudrate_250000(self, pty_pair: tuple[int, str]) -> None:
        """Test custom 250000 baudrate via BOTHER (experimental branch)."""
        master_fd, slave_name = pty_pair

        # 250000 is not in standard BAUDRATE_MAP but should work via termios2/BOTHER
        try:
            with TermiosSerial(slave_name, baudrate=250000) as ser:
                assert ser.is_open
                assert ser.baudrate == 250000
                # Verify we can still write/read (PTY doesn't care about baud)
                test_data = b"250k test"
                written = ser.write(test_data)
                assert written == len(test_data)
        except SerialException as e:
            # May fail on systems without termios2 BOTHER support
            if "termios2" in str(e) or "custom baudrate" in str(e).lower():
                pytest.skip("System doesn't support custom baudrates via BOTHER")
            raise

    def test_fileno(self, pty_pair: tuple[int, str]) -> None:
        """Test fileno() returns valid fd."""
        master_fd, slave_name = pty_pair

        with TermiosSerial(slave_name, baudrate=115200) as ser:
            fd = ser.fileno()
            assert isinstance(fd, int)
            assert fd == ser.fd

    def test_fileno_closed_raises(self) -> None:
        """Test fileno() on closed port raises."""
        ser = TermiosSerial("/dev/null", baudrate=115200, do_not_open=True)
        with pytest.raises(SerialException, match="not open"):
            ser.fileno()

    def test_read_timeout(self, pty_pair: tuple[int, str]) -> None:
        """Test read timeout returns empty bytes."""
        master_fd, slave_name = pty_pair

        with TermiosSerial(slave_name, baudrate=115200, timeout=0.1) as ser:
            # Don't write anything - read should timeout
            data = ser.read(10)
            assert data == b""

    def test_write_empty_returns_zero(self, pty_pair: tuple[int, str]) -> None:
        """Test writing empty data returns 0."""
        master_fd, slave_name = pty_pair

        with TermiosSerial(slave_name, baudrate=115200) as ser:
            assert ser.write(b"") == 0

    def test_read_zero_returns_empty(self, pty_pair: tuple[int, str]) -> None:
        """Test reading 0 bytes returns empty."""
        master_fd, slave_name = pty_pair

        with TermiosSerial(slave_name, baudrate=115200) as ser:
            assert ser.read(0) == b""

    def test_exclusive_mode(self, pty_pair: tuple[int, str]) -> None:
        """Test exclusive mode property."""
        master_fd, slave_name = pty_pair

        ser = TermiosSerial(slave_name, baudrate=115200, exclusive=True, do_not_open=True)
        assert ser.exclusive is True

        ser.exclusive = False
        assert ser.exclusive is False

        ser.open()
        ser.close()


class TestSerialForUrl:
    """Tests for serial_for_url factory function."""

    def test_returns_termios_serial(self) -> None:
        """Verify factory returns TermiosSerial instance."""
        ser = serial_for_url("/dev/null", baudrate=115200, do_not_open=True)
        assert isinstance(ser, TermiosSerial)
        assert ser.port == "/dev/null"

    def test_passes_kwargs(self) -> None:
        """Verify kwargs are passed correctly."""
        ser = serial_for_url(
            "/dev/null",
            baudrate=9600,
            timeout=1.5,
            exclusive=True,
            do_not_open=True,
        )
        assert ser.baudrate == 9600
        assert ser.timeout == 1.5
        assert ser.exclusive is True
        assert not ser.is_open


class TestEdgeCases:
    """Edge case and error handling tests."""

    def test_close_already_closed_is_safe(self) -> None:
        """Closing an already closed port should be safe."""
        ser = TermiosSerial("/dev/null", baudrate=115200, do_not_open=True)
        ser.close()  # Not open
        ser.close()  # Should not raise

    def test_double_open_is_safe(self, tmp_path: pytest.TempPathFactory) -> None:
        """Opening already open port should be a no-op."""
        master_fd, slave_fd = pty.openpty()
        slave_name = os.ttyname(slave_fd)
        os.close(slave_fd)

        try:
            ser = TermiosSerial(slave_name, baudrate=115200)
            fd1 = ser.fd
            ser.open()  # Already open
            assert ser.fd == fd1  # Same fd
            ser.close()
        finally:
            os.close(master_fd)

    def test_operations_on_closed_port_raise(self) -> None:
        """Operations on closed port should raise."""
        ser = TermiosSerial("/dev/null", baudrate=115200, do_not_open=True)

        with pytest.raises(SerialException, match="not open"):
            ser.read(10)

        with pytest.raises(SerialException, match="not open"):
            ser.write(b"test")

    def test_in_waiting_closed_returns_zero(self) -> None:
        """in_waiting on closed port returns 0."""
        ser = TermiosSerial("/dev/null", baudrate=115200, do_not_open=True)
        assert ser.in_waiting == 0

    def test_write_memoryview(self) -> None:
        """Test writing memoryview type."""
        master_fd, slave_fd = pty.openpty()
        slave_name = os.ttyname(slave_fd)
        os.close(slave_fd)

        try:
            with TermiosSerial(slave_name, baudrate=115200) as ser:
                data = memoryview(b"test")
                written = ser.write(data)
                assert written == 4
        finally:
            os.close(master_fd)

    def test_write_bytearray(self) -> None:
        """Test writing bytearray type."""
        master_fd, slave_fd = pty.openpty()
        slave_name = os.ttyname(slave_fd)
        os.close(slave_fd)

        try:
            with TermiosSerial(slave_name, baudrate=115200) as ser:
                data = bytearray(b"test")
                written = ser.write(data)
                assert written == 4
        finally:
            os.close(master_fd)
