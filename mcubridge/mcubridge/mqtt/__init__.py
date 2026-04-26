"""MQTT helpers for McuBridge.

This module exposes property-builder utilities consumed by the MQTT
transport layer and by external test fixtures.
"""

from __future__ import annotations

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties


def build_mqtt_connect_properties() -> Properties:
    """Return default CONNECT properties for aiomqtt/paho clients.

    [SIL-2] Explicit property construction for mission-critical connectivity.
    """

    props = Properties(PacketTypes.CONNECT)
    props.SessionExpiryInterval = 0
    props.RequestResponseInformation = 1
    props.RequestProblemInformation = 1
    return props


__all__ = [
    "build_mqtt_connect_properties",
]
