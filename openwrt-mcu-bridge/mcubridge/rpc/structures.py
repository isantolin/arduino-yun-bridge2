"""Binary data structures for RPC protocol payloads defined via Construct.

This module provides declarative definitions for complex binary payloads
used in the MCU Bridge protocol, replacing manual byte slicing and unpacking.
"""

from construct import Bytes, Int8ub, Int16ub, PaddedString, Struct, this

# --- Filesystem Packets ---

FileWritePacket = Struct(
    "path_len" / Int8ub,
    "path" / PaddedString(this.path_len, "utf-8"),
    "data_len" / Int16ub,
    "data" / Bytes(this.data_len),
)

FileReadPacket = Struct(
    "path_len" / Int8ub,
    "path" / PaddedString(this.path_len, "utf-8"),
)

FileRemovePacket = Struct(
    "path_len" / Int8ub,
    "path" / PaddedString(this.path_len, "utf-8"),
)

# --- System Packets ---

VersionResponsePacket = Struct(
    "major" / Int8ub,
    "minor" / Int8ub,
)
