import asyncio
from typing import Any, Callable, Coroutine

async def null_callback(*args: Any) -> None: ...
def wrap_func(func: Callable[..., Any]) -> Callable[..., Coroutine[Any, Any, Any]]:
    """wrap in a coroutine"""
    ...

class Cron:
    def __init__(
        self,
        spec: str,
        func: Callable[..., Any] | None = ...,
        args: tuple[Any, ...] = ...,
        kwargs: dict[str, Any] | None = ...,
        start: bool = ...,
        uuid: str | None = ...,
        loop: asyncio.AbstractEventLoop | None = ...,
        tz: Any | None = ...,
    ) -> None: ...
    def start(self) -> None:
        """Start scheduling"""
        ...

    def stop(self) -> None:
        """Stop scheduling"""
        ...

    async def next(self, *args: Any) -> Any:
        """yield from .next()"""
        ...

    def initialize(self) -> None:
        """Initialize cronsim and related times"""
        ...

    def get_next(self) -> float:
        """Return next iteration time related to loop time"""
        ...

    def call_next(self) -> None:
        """Set next hop in the loop. Call task"""
        ...

    def call_func(self, *args: Any, **kwargs: Any) -> None:
        """Called. Take care of exceptions using gather"""
        ...

    def set_result(self, result: Any) -> None:
        """Set future's result if needed (can be an exception).
        Else raise if needed."""
        ...

    def __call__(self, func: Callable[..., Any]) -> "Cron":
        """Used as a decorator"""
        ...

    def __str__(self) -> str: ...
    def __repr__(self) -> str: ...

def crontab(
    spec: str,
    func: Callable[..., Any] | None = ...,
    args: tuple[Any, ...] = ...,
    kwargs: dict[str, Any] | None = ...,
    start: bool = ...,
    loop: asyncio.AbstractEventLoop | None = ...,
    tz: Any | None = ...,
) -> "Cron": ...
