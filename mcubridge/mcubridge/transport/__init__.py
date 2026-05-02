"""Transport abstractions (serial) for the MCU Bridge daemon."""

from .serial_link import (
    write_frame,
    open_serial_link,
)

__all__ = [
    "write_frame",
    "open_serial_link",
]
