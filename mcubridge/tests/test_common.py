"""Common utility tests for the MCU Bridge daemon."""

from __future__ import annotations


from mcubridge.mqtt import (
    build_mqtt_connect_properties,
)


def test_build_mqtt_connect_properties_sets_flags() -> None:
    props = build_mqtt_connect_properties()
    # Session expiry set to 1 hour
    assert props.SessionExpiryInterval == 3600
    assert props.MaximumPacketSize == 65535
