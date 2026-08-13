"""Type stub for protovalidate package [SIL-2]."""

from typing import Any
from google.protobuf.message import Message

class ValidationError(Exception):
    violations: Any
    def __init__(self, msg: str = ..., violations: Any = ..., *args: Any, **kwargs: Any) -> None: ...
    def to_proto(self) -> Any: ...

def validate(message: Message, **kwargs: Any) -> None: ...

__all__ = ["validate", "ValidationError"]
