from __future__ import annotations

from typing import Any, Generic, TypeVar
from collections.abc import Iterator

_T = TypeVar("_T")


class Deque(Generic[_T]):
    def __init__(self,
                 *,
                 directory: str | None = None,
                 **kwargs: Any) -> None: ...

    def append(self, value: _T) -> None: ...

    def popleft(self) -> _T: ...

    def clear(self) -> None: ...

    def close(self) -> None: ...

    def __len__(self) -> int: ...

    def __iter__(self) -> Iterator[_T]: ...


__all__ = ["Deque"]
