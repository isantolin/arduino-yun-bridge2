"""Base interfaces for service components. [Eradicated Indirection]"""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from aiomqtt.message import Message
    from ..protocol.structures import QueuedPublish
    from ..state.context import RuntimeState
    from ..config.settings import RuntimeConfig
    from .serial_flow import SerialFlowController

logger = structlog.get_logger("mcubridge.service")


class MqttFlow(Protocol):
    """Protocol for MQTT interactions. [SIL-2]"""

    async def enqueue_mqtt(
        self,
        message: QueuedPublish,
        *,
        reply_context: Message | None = None,
    ) -> None:
        """Enqueues an MQTT message for publishing."""
        ...

    async def publish(
        self,
        topic: str,
        payload: bytes | str,
        *,
        qos: int = 0,
        retain: bool = False,
        expiry: int | None = None,
        properties: tuple[tuple[str, str], ...] = (),
        content_type: str | None = None,
        reply_to: Message | None = None,
    ) -> None:
        """Convenience method for publishing."""
        ...


class BridgeComponent:
    """Base class for all bridge service components."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        serial_flow: SerialFlowController,
        mqtt_flow: MqttFlow,
    ) -> None:
        self.config = config
        self.state = state
        self.serial_flow = serial_flow
        self.mqtt_flow = mqtt_flow
