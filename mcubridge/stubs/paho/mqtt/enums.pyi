"""Manual stub for paho.mqtt.enums."""

from __future__ import annotations

import enum

class MQTTProtocolVersion(enum.IntEnum):
    MQTTv31: int
    MQTTv311: int
    MQTTv5: int

class CallbackAPIVersion(enum.Enum):
    VERSION1: int
    VERSION2: int
