"""Type stub for protovalidate package [SIL-2]."""

from typing import Any
from google.protobuf.message import Message

class ValidationError(Exception):
    violations: Any
    def to_proto(self) -> Any: ...

def validate(message: Message, **kwargs: Any) -> None: ...

__all__ = ["validate", "ValidationError"]
