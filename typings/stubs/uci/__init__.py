"""Manual stub for OpenWrt UCI."""

from __future__ import annotations
from types import TracebackType
from typing import Any

class Uci:
    def __init__(self) -> None:
        pass

    def __enter__(self) -> Uci:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        return False

    def get_all(self, package: str, section: str | None = None) -> Any:
        return {}

    def get(self, package: str, section: str, option: str) -> str:
        return ""

    def set(self, package: str, section: str, option: str, value: str) -> None:
        pass

    def commit(self, package: str) -> None:
        pass


class UciException(Exception):
    pass
