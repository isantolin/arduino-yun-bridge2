"""Type stub for vulture package [SIL-2]."""

from collections.abc import Sequence

class Item:
    name: str
    filename: str
    first_lineno: int
    last_lineno: int
    message: str
    confidence: int

class Vulture:
    def __init__(
        self,
        verbose: bool = False,
        ignore_names: Sequence[str] | None = None,
        ignore_decorators: Sequence[str] | None = None,
    ) -> None: ...
    def scavenge(self, paths: Sequence[str], exclude: Sequence[str] | None = None) -> None: ...
    def scan(self, code: str, filename: str = "") -> None: ...
    def get_unused_code(self, min_confidence: int = 0, sort_by_size: bool = False) -> list[Item]: ...

__all__ = ["Vulture", "Item"]
