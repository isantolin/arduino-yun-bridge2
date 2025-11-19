"""Shared constants for Yun Bridge daemon components."""
from __future__ import annotations

from ssl import TLSVersion

SERIAL_TERMINATOR: bytes = b"\x00"
STATUS_FILE_PATH: str = "/tmp/yunbridge_status.json"
ALLOWED_COMMAND_WILDCARD: str = "*"
MQTT_TLS_MIN_VERSION: TLSVersion = TLSVersion.TLSv1_2

__all__ = [
    "SERIAL_TERMINATOR",
    "STATUS_FILE_PATH",
    "ALLOWED_COMMAND_WILDCARD",
    "MQTT_TLS_MIN_VERSION",
]
