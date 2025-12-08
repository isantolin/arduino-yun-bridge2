from __future__ import annotations

import asyncio
from typing import Any


async def open_serial_connection(
    *,
    loop: asyncio.AbstractEventLoop | None = ...,
    limit: int | None = ...,
    **kwargs: Any,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]: ...

__all__ = ["open_serial_connection"]
