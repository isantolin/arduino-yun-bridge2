"""High-level SPI abstraction for MCU Bridge client."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from . import mcubridge_pb2 as pb
from .mcubridge_grpc import LocalBridgeStub
from .protocol import SpiBitOrder, SpiDataMode

SpiBuffer = bytes | bytearray | Sequence[int]


class SpiDevice:
    """A high-level SPI device interface interacting directly with LocalBridgeStub.

    Handles automatic initialization (begin/end) and provides a clean
    API for full-duplex transfers.
    """

    def __init__(
        self,
        stub: LocalBridgeStub,
        frequency: int = 4000000,
        bit_order: SpiBitOrder = SpiBitOrder.SPI_BIT_ORDER_MSB_FIRST,
        mode: SpiDataMode = SpiDataMode.SPI_DATA_MODE_0,
    ):
        self._stub = stub
        self.frequency = frequency
        self.bit_order = bit_order
        self.mode = mode
        self.is_active = False

    async def __aenter__(self) -> SpiDevice:
        """Enter context: begin SPI session."""
        await self.begin()
        return self

    async def __aexit__(self, *args: object) -> None:
        """Exit context: end SPI session."""
        await self.end()

    async def begin(self) -> None:
        """Initialize the SPI bus on the MCU."""
        if self.is_active:
            return
        cfg = pb.SpiConfig(
            frequency=self.frequency,
            bit_order=pb.SpiBitOrder.Value(self.bit_order.name),
            data_mode=pb.SpiDataMode.Value(self.mode.name),
        )
        await self._stub.SpiConfigure(cfg)
        self.is_active = True

    async def end(self) -> None:
        """Deinitialize the SPI bus on the MCU."""
        self.is_active = False

    async def transfer(self, data: SpiBuffer) -> bytes:
        """Perform a full-duplex SPI transfer."""
        if not self.is_active:
            await self.begin()

        payload = cast(bytes, data) if isinstance(data, (bytes, bytearray)) else bytes(data)

        resp = await self._stub.SpiTransfer(pb.SpiTransfer(data=payload))
        return resp.data
