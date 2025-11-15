"""Minimal MQTT client used when the bridge package is unavailable."""
from __future__ import annotations

try:
    from yunbridge.mqtt.client import (
        AccessRefusedError,
        Client,
        ConnectionCloseForcedError,
        ConnectionLostError,
        ConnectResult,
        DeliveredMessage,
        MQTTError,
        PublishableMessage,
        QOSLevel,
    )
except ImportError:  # pragma: no cover - fallback for standalone examples
    from ._mqtt_asyncio import (
        AccessRefusedError,
        Client,
        ConnectionCloseForcedError,
        ConnectionLostError,
        ConnectResult,
        DeliveredMessage,
        MQTTError,
        PublishableMessage,
        QOSLevel,
    )


__all__ = [
    "Client",
    "ConnectResult",
    "PublishableMessage",
    "DeliveredMessage",
    "QOSLevel",
    "MQTTError",
    "AccessRefusedError",
    "ConnectionLostError",
    "ConnectionCloseForcedError",
]
