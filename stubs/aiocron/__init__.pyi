from __future__ import annotations

import asyncio
from typing import Any
from collections.abc import Awaitable, Callable, Mapping, Sequence

CronCallback = Callable[[], Awaitable[None]]


class Cron:
    def __init__(self,
                 spec: str,
                 func: CronCallback | None = None,
                 *,
                 args: Sequence[Any] = ...,
                 kwargs: Mapping[str, Any] | None = None,
                 start: bool = True,
                 loop: asyncio.AbstractEventLoop | None = None,
                 tz: Any | None = None) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


def crontab(
    spec: str,
    func: CronCallback | None = None,
    *,
    args: Sequence[Any] = ...,
    kwargs: Mapping[str, Any] | None = None,
    start: bool = True,
    loop: asyncio.AbstractEventLoop | None = None,
    tz: Any | None = None,
) -> Cron: ...
