"""Type stubs for esprima package."""

from typing import Any

class Error(Exception):
    lineNumber: int
    column: int
    index: int
    message: str
    description: str

def parseScript(
    code: str,
    options: dict[str, Any] | None = None,
    delegate: Any = None,
) -> Any: ...

def parseModule(
    code: str,
    options: dict[str, Any] | None = None,
    delegate: Any = None,
) -> Any: ...
