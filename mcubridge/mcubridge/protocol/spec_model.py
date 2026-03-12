"""Protocol spec data model — pure parsing, zero dependency on generated code.

This module exists to break the circular dependency between the code generator
(``tools/protocol/generate.py``) and the generated ``protocol.py``.  The
generator needs :class:`ProtocolSpec` to read ``spec.toml``, while the rest of
the protocol package imports symbols *from* the generated module.  By keeping
the spec model in its own file with **no** relative imports, the generator can
load it via :mod:`importlib.util` without triggering the package
``__init__.py``.
"""

from pathlib import Path
from typing import Any

import msgspec


# =============================================================================
# Protocol Generation Structures (msgspec)
# =============================================================================


class CommandDef(msgspec.Struct, frozen=True):
    name: str
    value: int
    directions: list[str]
    category: str | None = None
    description: str | None = None
    requires_ack: bool = False
    expects_direct_response: bool = False


class StatusDef(msgspec.Struct, frozen=True):
    name: str
    value: int
    description: str


class StructField(msgspec.Struct, frozen=True):
    name: str
    type_code: str  # B, H, I, Q

    @property
    def cpp_type(self) -> str:
        return {"B": "uint8_t", "H": "uint16_t", "I": "uint32_t", "Q": "uint64_t"}[self.type_code]

    @property
    def size(self) -> int:
        return {"B": 1, "H": 2, "I": 4, "Q": 8}[self.type_code]

    @property
    def read_func(self) -> str | None:
        return {
            "B": None,
            "H": "rpc::read_u16_be",
            "I": "rpc::read_u32_be",
            "Q": "rpc::read_u64_be",
        }[self.type_code]

    @property
    def write_func(self) -> str | None:
        func = self.read_func
        return func.replace("read_", "write_") if func else None


class PayloadDef(msgspec.Struct, frozen=True):
    name: str
    fields: list[StructField]

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.fields)

    @property
    def all_bytes(self) -> bool:
        return all(not f.read_func for f in self.fields)

    @property
    def byte_inits(self) -> str:
        return ", ".join(f"data[{i}]" for i in range(len(self.fields)))


class RawProtocolData(msgspec.Struct):
    constants: dict[str, Any]
    commands: list[dict[str, Any]]
    statuses: list[dict[str, Any]]
    payloads: dict[str, dict[str, str]]
    handshake: dict[str, Any]
    mqtt_subscriptions: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    topics: list[dict[str, Any]]
    capabilities: dict[str, int]
    architectures: dict[str, int]
    status_reasons: dict[str, str]


class ProtocolSpec(msgspec.Struct):
    """Root model of the parsed spec.toml."""

    constants: dict[str, Any]
    commands: list[CommandDef]
    statuses: list[StatusDef]
    payloads: dict[str, PayloadDef]
    handshake: dict[str, Any]
    mqtt_subscriptions: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    topics: list[dict[str, Any]]
    capabilities: dict[str, int]
    architectures: dict[str, int]
    status_reasons: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> "ProtocolSpec":
        import msgspec.toml

        with path.open("rb") as f:
            raw = msgspec.toml.decode(f.read(), type=RawProtocolData)

        # Convert raw dicts to Structs
        cmds = [msgspec.convert(c, CommandDef) for c in raw.commands]
        statuses = [msgspec.convert(s, StatusDef) for s in raw.statuses]

        pls: dict[str, PayloadDef] = {}
        for name, fields_dict in raw.payloads.items():
            fields = [StructField(name=k, type_code=v) for k, v in fields_dict.items()]
            pls[name] = PayloadDef(name=name, fields=fields)

        return cls(
            constants=raw.constants,
            commands=cmds,
            statuses=statuses,
            payloads=pls,
            handshake=raw.handshake,
            mqtt_subscriptions=raw.mqtt_subscriptions,
            actions=raw.actions,
            topics=raw.topics,
            capabilities=raw.capabilities,
            architectures=raw.architectures,
            status_reasons=raw.status_reasons,
        )
