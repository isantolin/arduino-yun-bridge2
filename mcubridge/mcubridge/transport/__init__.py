"""Transport abstractions (serial, MQTT) for the MCU Bridge daemon."""

from .mqtt import MqttTransport
from .serial import (
    MAX_SERIAL_FRAME_BYTES,
    SerialTransport,
)

__all__ = [
    "MAX_SERIAL_FRAME_BYTES",
    "MqttTransport",
    "SerialTransport",
]
