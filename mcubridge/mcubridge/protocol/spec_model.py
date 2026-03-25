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


class RawProtocolData(msgspec.Struct):
    constants: dict[str, Any]
    hardware: dict[str, Any]
    commands: list[dict[str, Any]]
    statuses: list[dict[str, Any]]
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
    hardware: dict[str, Any]
    commands: list[CommandDef]
    statuses: list[StatusDef]
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

        return cls(
            constants=raw.constants,
            hardware=raw.hardware,
            commands=cmds,
            statuses=statuses,
            handshake=raw.handshake,
            mqtt_subscriptions=raw.mqtt_subscriptions,
            actions=raw.actions,
            topics=raw.topics,
            capabilities=raw.capabilities,
            architectures=raw.architectures,
            status_reasons=raw.status_reasons,
        )
