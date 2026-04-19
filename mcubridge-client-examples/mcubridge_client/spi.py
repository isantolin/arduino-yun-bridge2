"""High-level SPI abstraction for MCU Bridge client using direct MQTT."""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Sequence, Union, cast

import msgspec
from aiomqtt import Client
from .definitions import SpiBitOrder, SpiMode
from .protocol import Topic

SpiBuffer = Union[bytes, bytearray, Sequence[int]]


class SpiDevice:
    """A high-level SPI device interface over direct MQTT."""

    def __init__(
        self,
        client: Client,
        frequency: int = 4000000,
        bit_order: SpiBitOrder = SpiBitOrder.MSBFIRST,
        mode: SpiMode = SpiMode.MODE0,
    ):
        self._client = client
        self._frequency = frequency
        self._bit_order = bit_order
        self._mode = mode
        self._active = False

    async def __aenter__(self) -> SpiDevice:
        await self.begin()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.end()

    async def begin(self) -> None:
        if self._active:
            return
        await self._client.publish(str(Topic.build(Topic.SPI, "begin")), b"")
        
        config = {
            "frequency": self._frequency,
            "bit_order": self._bit_order.value,
            "data_mode": self._mode.value,
        }
        await self._client.publish(
            str(Topic.build(Topic.SPI, "config")), msgspec.json.encode(config)
        )
        self._active = True

    async def end(self) -> None:
        if not self._active:
            return
        await self._client.publish(str(Topic.build(Topic.SPI, "end")), b"")
        self._active = False

    async def transfer(self, data: SpiBuffer) -> bytes:
        if not self._active:
            await self.begin()

        payload = bytes(data) if not isinstance(data, (bytes, bytearray)) else bytes(data)
        
        transfer_topic = str(Topic.build(Topic.SPI, "transfer"))
        resp_topic = str(Topic.build(Topic.SPI, "transfer", "resp"))
        
        await self._client.subscribe(resp_topic)
        await self._client.publish(transfer_topic, payload)
        
        try:
            async with asyncio.timeout(5.0):
                async for message in self._client.messages:
                    if Topic.matches(resp_topic, str(message.topic)):
                        return bytes(message.payload) if message.payload else b""
        except asyncio.TimeoutError:
            return b""
        finally:
            await self._client.unsubscribe(resp_topic)
        return b""
