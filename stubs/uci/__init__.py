"""UCI stub for testing."""

from __future__ import annotations
from typing import Any


class UciException(Exception):
    pass


class Uci:
    def __enter__(self) -> Uci:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def get_all(self, package: str, section: str) -> dict[str, Any]:
        return {}
