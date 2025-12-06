from typing import Any, Mapping, Optional, Type, TypeVar


class UciException(Exception):
    ...


_T = TypeVar("_T")


class Uci:
    def __enter__(self: _T) -> _T: ...

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[Any],
    ) -> None: ...

    def get_all(self, package: str, section: str) -> Mapping[str, Any]: ...


def UciCursor() -> Uci:
    ...


def Uci() -> Uci:
    ...


__all__ = ["Uci", "UciException"]
