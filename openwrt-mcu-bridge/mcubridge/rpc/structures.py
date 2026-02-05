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

FreeMemoryResponsePacket = Struct(
    "value" / Int16ub,
)

# --- Pin Packets ---

DigitalReadResponsePacket = Struct(
    "value" / Int8ub,
)

AnalogReadResponsePacket = Struct(
    "value" / Int16ub,
)

# --- Datastore Packets ---

DatastoreGetPacket = Struct(
    "key_len" / Int8ub,
    "key" / PaddedString(this.key_len, "utf-8"),
)

DatastorePutPacket = Struct(
    "key_len" / Int8ub,
    "key" / PaddedString(this.key_len, "utf-8"),
    "value_len" / Int8ub,
    "value" / Bytes(this.value_len),
)

# --- Mailbox Packets ---

MailboxPushPacket = Struct(
    "msg_len" / Int16ub,
    "data" / Bytes(this.msg_len),
)
