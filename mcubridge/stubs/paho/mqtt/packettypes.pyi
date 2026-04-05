"""Manual stub for paho.mqtt.packettypes."""

from __future__ import annotations

class PacketTypes:
    CONNECT: int
    CONNACK: int
    PUBLISH: int
    PUBACK: int
    PUBREC: int
    PUBREL: int
    PUBCOMP: int
    SUBSCRIBE: int
    SUBACK: int
    UNSUBSCRIBE: int
    UNSUBACK: int
    PINGREQ: int
    PINGRESP: int
    DISCONNECT: int
    AUTH: int
    WILLMESSAGE: int
    Names: tuple[str, ...]
    indexes: range
