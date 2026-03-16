from typing import Any, TypeVar, Type, Callable, Awaitable

T = TypeVar("T")

class Registry:
    def register_value(
        self,
        svc_type: Type[T],
        value: T,
        *,
        enter: bool = ...,
        ping: Callable[..., Any] | None = ...,
        on_registry_close: Callable[..., Any] | Awaitable[Any] | None = ...,
    ) -> None: ...
    def register_factory(
        self,
        svc_type: Type[T],
        factory: Callable[..., T | Awaitable[T]],
        *,
        enter: bool = ...,
        ping: Callable[..., Any] | None = ...,
        on_registry_close: Callable[..., Any] | Awaitable[Any] | None = ...,
    ) -> None: ...

class Container:
    def __init__(self, registry: Registry) -> None: ...
    def get(self, svc_type: Type[T]) -> T: ...
    async def get_abstract(self, svc_type: Type[T]) -> T: ...
    def close(self) -> None: ...
    async def aclose(self) -> None: ...
