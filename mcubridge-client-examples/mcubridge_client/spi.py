"""High-level SPI abstraction for MCU Bridge client."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union, cast

from . import mcubridge_pb2 as pb
from .definitions import SpiBitOrder, SpiMode
from .mcubridge_grpc import LocalBridgeStub

SpiBuffer = Union[bytes, bytearray, Sequence[int]]


class SpiDevice:
    """A high-level SPI device interface interacting directly with LocalBridgeStub.

    Handles automatic initialization (begin/end) and provides a clean
    API for full-duplex transfers.
    """

    def __init__(
        self,
        stub: LocalBridgeStub,
        frequency: int = 4000000,
        bit_order: SpiBitOrder = SpiBitOrder.MSBFIRST,
        mode: SpiMode = SpiMode.MODE0,
        topic_prefix: str = "br",
    ):
        self._stub = stub
        self._frequency = frequency
        self._bit_order = bit_order
        self._mode = mode
        self._topic_prefix = topic_prefix
        self._active = False

    async def __aenter__(self) -> SpiDevice:
        """Enter context: begin SPI session."""
        await self.begin()
        return self

    async def __aexit__(self, *args: object) -> None:
        """Exit context: end SPI session."""
        await self.end()

    async def begin(self) -> None:
        """Initialize the SPI bus on the MCU."""
        if self._active:
            return
        cfg = pb.SpiConfig(
            frequency=self._frequency,
            bit_order=self._bit_order.value,
            data_mode=self._mode.value,
        )
        await self._stub.SpiConfigure(cfg)
        self._active = True

    async def end(self) -> None:
        """Deinitialize the SPI bus on the MCU."""
        self._active = False

    async def transfer(self, data: SpiBuffer) -> bytes:
        """Perform a full-duplex SPI transfer."""
        if not self._active:
            await self.begin()

        if isinstance(data, (bytes, bytearray)):
            payload = cast(bytes, data)
        else:
            payload = bytes(data)

        resp = await self._stub.SpiTransfer(pb.SpiTransfer(data=payload))
        return resp.data if resp.data else payload

    @property
    def frequency(self) -> int:
        return self._frequency

    @property
    def bit_order(self) -> SpiBitOrder:
        return self._bit_order

    @property
    def mode(self) -> SpiMode:
        return self._mode
