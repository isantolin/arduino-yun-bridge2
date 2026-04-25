"""Transport layer for the MCU Bridge daemon."""

from __future__ import annotations

from .serial import SerialTransport

__all__ = [
    "SerialTransport",
]
