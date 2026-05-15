"""Transport abstractions (serial, MQTT) for the MCU Bridge daemon."""

from .serial import (
    SerialTransport,
)

__all__ = [
    "SerialTransport",
]
