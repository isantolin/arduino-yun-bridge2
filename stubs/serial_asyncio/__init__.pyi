from __future__ import annotations

import asyncio
from typing import Any, Tuple


async def open_serial_connection(
    *,
    loop: asyncio.AbstractEventLoop | None = ...,
    limit: int | None = ...,
    **kwargs: Any,
) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]: ...

__all__ = ["open_serial_connection"]
