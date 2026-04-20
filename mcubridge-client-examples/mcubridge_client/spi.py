"""High-level SPI abstraction for MCU Bridge client."""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Sequence, Union, cast

from .definitions import SpiBitOrder, SpiMode

if TYPE_CHECKING:
    from . import Bridge

SpiBuffer = Union[bytes, bytearray, Sequence[int]]


class SpiDevice:
    """A high-level SPI device interface.

    Handles automatic initialization (begin/end) and provides a clean
    API for full-duplex transfers.
    """

    def __init__(
        self,
        bridge: Bridge,
        frequency: int = 4000000,
        bit_order: SpiBitOrder = SpiBitOrder.MSBFIRST,
        mode: SpiMode = SpiMode.MODE0,
    ):
        self._bridge = bridge
        self._frequency = frequency
        self._bit_order = bit_order
        self._mode = mode
        self._active = False

    async def __aenter__(self) -> SpiDevice:
        """Enter context: begin SPI session."""
        await self.begin()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit context: end SPI session."""
        await self.end()

    async def begin(self) -> None:
        """Initialize the SPI bus on the MCU."""
        if self._active:
            return
        await self._bridge.spi_begin()
        await self._bridge.spi_config(
            frequency=self._frequency,
            bit_order=self._bit_order.value,
            data_mode=self._mode.value,
        )
        self._active = True

    async def end(self) -> None:
        """Deinitialize the SPI bus on the MCU."""
        if not self._active:
            return
        await self._bridge.spi_end()
        self._active = False

    async def transfer(self, data: SpiBuffer) -> bytes:
        """Perform a full-duplex SPI transfer.

        Args:
            data: Data to send. Can be bytes, bytearray, or a list of integers.

        Returns:
            The data received from the MCU during the transfer.
        """
        if not self._active:
            await self.begin()

        if isinstance(data, (bytes, bytearray)):
            payload = cast(bytes, data)
        else:
            payload = bytes(data)

        return await self._bridge.spi_transfer(payload)

    @property
    def frequency(self) -> int:
        return self._frequency

    @property
    def bit_order(self) -> SpiBitOrder:
        return self._bit_order

    @property
    def mode(self) -> SpiMode:
        return self._mode
