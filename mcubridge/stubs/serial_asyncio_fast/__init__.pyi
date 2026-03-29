"""Type stubs for serial_asyncio_fast."""
import asyncio
from typing import Any, Callable

class SerialTransport(asyncio.Transport):
    def is_closing(self) -> bool: ...
    def close(self) -> None: ...
    def get_extra_info(self, name: str, default: Any = None) -> Any: ...

async def open_serial_connection(
    url: str,
    *args: Any,
    **kwargs: Any,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]: ...

async def create_serial_connection(
    loop: asyncio.AbstractEventLoop,
    protocol_factory: Callable[[], asyncio.Protocol],
    url: str,
    *args: Any,
    **kwargs: Any,
) -> tuple[SerialTransport, asyncio.Protocol]: ...
