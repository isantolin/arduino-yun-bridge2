"""Compatibility helpers for Paho MQTT across major versions."""
from __future__ import annotations

import logging
from types import ModuleType
from typing import Any

try:
    import paho.mqtt.client as mqtt_client
except ImportError:  # pragma: no cover - dependency missing during typing
    mqtt: ModuleType | None = None
else:
    mqtt = mqtt_client
    _LOGGER = logging.getLogger("yunbridge_client.paho_compat")

    def _noop(*_: Any, **__: Any) -> None:
        return None

    if not hasattr(mqtt.Client, "message_retry_set"):

        def _message_retry_set(self: Any, value: int) -> None:
            _LOGGER.debug(
                "Ignoring message_retry_set=%s request; feature removed in "
                "Paho >= 2",
                value,
            )

        setattr(mqtt.Client, "message_retry_set", _message_retry_set)

    # Provide a friendlier attribute for older aiomqtt releases so they
    # don't warn repeatedly when running on newer Paho. Log only once.
    if getattr(mqtt.Client, "message_retry_set", _noop) is _noop:
        _LOGGER.debug("message_retry_set shim already present")


def ensure_compat() -> None:
    """Explicit no-op to make the module usage visible to type checkers."""


__all__ = ["mqtt", "ensure_compat"]
