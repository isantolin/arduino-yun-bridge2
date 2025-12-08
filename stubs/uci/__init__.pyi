from typing import Any, TypeVar
from collections.abc import Mapping


class UciException(Exception):
    ...


_T = TypeVar("_T")


class Uci:
    def __enter__(self: _T) -> _T: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any | None,
    ) -> None: ...

    def get_all(self, package: str, section: str) -> Mapping[str, Any]: ...


def UciCursor() -> Uci:
    ...


def Uci() -> Uci:
    ...


__all__ = ["Uci", "UciException"]
