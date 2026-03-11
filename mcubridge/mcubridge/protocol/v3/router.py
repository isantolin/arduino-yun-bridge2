"""Virtual Channels Multiplexer for Bridge V3 PoC."""

import asyncio
import logging
from enum import IntEnum

import msgspec

logger = logging.getLogger(__name__)

class Endpoint(IntEnum):
    SYS = 0
    CTRL = 1
    DATA = 2
    BULK = 3

class V3Header(msgspec.Struct, frozen=True):
    type_flag: int
    compressed: int
    endpoint: Endpoint
    sequence: int

class V3VirtualRouter:
    """Simulates prioritized multiplexing on the MPU (Python) side."""

    def __init__(self):
        # Priority queues
        self.queues = {
            Endpoint.SYS: asyncio.Queue(maxsize=10),
            Endpoint.CTRL: asyncio.Queue(maxsize=20),
            Endpoint.DATA: asyncio.Queue(maxsize=20),
            Endpoint.BULK: asyncio.Queue(maxsize=5),
        }

    async def route_incoming(self, header: V3Header, payload: bytes) -> bool:
        q = self.queues.get(header.endpoint)
        if q is None:
            return False
            
        try:
            # Non-blocking put to simulate fast UART interrupt handler
            q.put_nowait((header, payload))
            return True
        except asyncio.QueueFull:
            logger.warning(f"Queue full for endpoint {header.endpoint.name}")
            return False

    async def get_next_priority_message(self) -> tuple[V3Header, bytes] | None:
        """Poll queues in strict priority order: SYS > CTRL > DATA > BULK."""
        for ep in [Endpoint.SYS, Endpoint.CTRL, Endpoint.DATA, Endpoint.BULK]:
            q = self.queues[ep]
            if not q.empty():
                return q.get_nowait()
        return None
