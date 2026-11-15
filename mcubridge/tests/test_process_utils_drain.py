"""PTY pre-drain must hand back the target's boot output instead of discarding it."""

from __future__ import annotations

import os
import pty
from pathlib import Path

from tools.emulation.process_utils import drain_pty_nonblocking


def test_drain_pty_returns_boot_output() -> None:
    master, slave = pty.openpty()
    try:
        os.write(master, b"ESP-ROM:esp32s3-20210327\r\nboot:0x8 (SPI_FAST_FLASH_BOOT)\r\n")
        drained = drain_pty_nonblocking(os.ttyname(slave), timeout=0.2)
        assert b"ESP-ROM:esp32s3" in drained
        assert b"SPI_FAST_FLASH_BOOT" in drained
    finally:
        os.close(master)
        os.close(slave)


def test_drain_pty_missing_device_returns_empty(tmp_path: Path) -> None:
    assert drain_pty_nonblocking(tmp_path / "absent", timeout=0.05) == b""
