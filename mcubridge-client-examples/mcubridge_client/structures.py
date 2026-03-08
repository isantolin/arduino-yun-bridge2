"""Data structures for the MCU Bridge client."""

from __future__ import annotations

from typing import Annotated, Any

import construct
import msgspec

# [SIL-2] Constants for deterministic parsing
UINT8_STRUCT = construct.Int8ub
UINT16_STRUCT = construct.Int16ub


class BaseStruct(msgspec.Struct):
    """Base class for all binary structures."""

    @classmethod
    def decode(cls, data: bytes, command_id: int | None = None) -> Any:
        """Decode binary data into a typed struct."""
        schema = getattr(cls, "SCHEMA", None)
        if schema is None:
            raise NotImplementedError(f"{cls.__name__} must define a SCHEMA.")
        try:
            raw = schema.parse(data)
            return msgspec.convert(dict(raw), cls)
        except (construct.ConstructError, msgspec.ValidationError) as exc:
            raise ValueError(f"Failed to decode {cls.__name__}: {exc}") from exc


def BinStruct(*args: Any, **kwargs: Any) -> construct.Struct:
    """Helper to create a construct Struct."""
    return construct.Struct(*args, **kwargs)


class DigitalReadResponsePacket(BaseStruct):
    pin: Annotated[int, msgspec.Meta(ge=0)]
    value: Annotated[int, msgspec.Meta(ge=0)]

    SCHEMA = BinStruct("pin" / UINT8_STRUCT, "value" / UINT8_STRUCT)


class AnalogReadResponsePacket(BaseStruct):
    pin: Annotated[int, msgspec.Meta(ge=0)]
    value: Annotated[int, msgspec.Meta(ge=0)]

    SCHEMA = BinStruct("pin" / UINT8_STRUCT, "value" / UINT16_STRUCT)
