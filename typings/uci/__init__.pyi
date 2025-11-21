from __future__ import annotations

from typing import Any, Dict


class Uci:
    def __enter__(self) -> "Uci": ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    def get_all(self, package: str, section: str) -> Dict[str, Any]: ...


class UciException(Exception):
    ...


__all__ = ["Uci", "UciException"]
