"""Mock implementations for McuBridge tests."""

from __future__ import annotations

from typing import Any, Coroutine

from mcubridge.protocol.spec_model import QueuedPublish
from mcubridge.state.context import RuntimeState


class DummyBridge:
    """Mock for the Serial Transport layer."""

    def __init__(self) -> None:
        self.sent_frames: list[tuple[int, bytes]] = []
        self.published: list[tuple[str, bytes | str, int, bool]] = []
        self.background_tasks: list[Coroutine[Any, Any, None]] = []
        self.is_connected: bool = True

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        self.sent_frames.append((command_id, payload))
        return True

    async def publish(
        self,
        topic: str,
        payload: bytes | str,
        qos: int = 0,
        retain: bool = False,
        reply_to: Any | None = None,
    ) -> None:
        self.published.append((topic, payload, qos, retain))

    async def enqueue_mqtt(
        self, message: QueuedPublish, reply_context: Any | None = None
    ) -> None:
        self.published.append(
            (message.topic_name, message.payload, message.qos, message.retain)
        )


class DummyContext:
    """Mock for the BridgeContext container."""

    def __init__(self, config: Any, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self.bridge = DummyBridge()
        self.send_frame = self.bridge.send_frame
        self.publish = self.bridge.publish
        self.enqueue_mqtt = self.bridge.enqueue_mqtt

    async def acknowledge_mcu_frame(self, cmd: int, seq: int, status: int) -> None:
        pass
