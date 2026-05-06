"""Manual stub for OpenWrt UCI."""

from __future__ import annotations
from typing import Any, Optional, Dict, overload
from types import TracebackType

class Uci:
    def __init__(self) -> None: pass
    def __enter__(self) -> Uci: return self
    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> bool: return False
    def get_all(self, package: str, section: Optional[str] = None) -> Any: return {}
    def get(self, package: str, section: str, option: str) -> str: return ""
    def set(self, package: str, section: str, option: str, value: str) -> None: pass
    def commit(self, package: str) -> None: pass

class UciException(Exception): pass
