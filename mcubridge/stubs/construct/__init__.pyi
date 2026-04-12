"""High-level manual stub for Construct."""

from __future__ import annotations
from typing import Any

class Construct:
    def parse(self, data: bytes | bytearray | memoryview, **contextkw: Any) -> Any: ...
    def build(self, obj: Any, **contextkw: Any) -> bytes: ...
    def sizeof(self, **contextkw: Any) -> int: ...
    def __truediv__(self, other: Any) -> Construct: ...
    def __rtruediv__(self, other: str) -> Construct: ...
    def __getattr__(self, name: str) -> Any: ...

# We define these as Any to allow both callability and operator support
# (which Pyright sometimes struggles with in complex overloads)
Struct: Any
BitStruct: Any
BitsInteger: Any
Flag: Any
Int8ub: Any
Int16ub: Any
Int32ub: Any
Int16ul: Any
Int64ub: Any
Enum: Any
RawCopy: Any
Const: Any
Select: Any
Terminated: Any
FocusedSeq: Any
GreedyRange: Any
Padding: Any
Bytes: Any
GreedyBytes: Any
Computed: Any
Check: Any
ExprAdapter: Any
Checksum: Any

class Adapter(Construct):
    def __init__(self, subcon: Construct) -> None: ...

this: Any = ...
