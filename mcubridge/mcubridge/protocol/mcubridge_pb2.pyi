from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RpcContainer(_message.Message):
    __slots__ = ("seq_id", "checksum", "payload")
    SEQ_ID_FIELD_NUMBER: _ClassVar[int]
    CHECKSUM_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    seq_id: int
    checksum: int
    payload: bytes
    def __init__(self, seq_id: _Optional[int] = ..., checksum: _Optional[int] = ..., payload: _Optional[bytes] = ...) -> None: ...

class VersionResponse(_message.Message):
    __slots__ = ("major", "minor", "patch")
    MAJOR_FIELD_NUMBER: _ClassVar[int]
    MINOR_FIELD_NUMBER: _ClassVar[int]
    PATCH_FIELD_NUMBER: _ClassVar[int]
    major: int
    minor: int
    patch: int
    def __init__(self, major: _Optional[int] = ..., minor: _Optional[int] = ..., patch: _Optional[int] = ...) -> None: ...

class FreeMemoryResponse(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: int
    def __init__(self, value: _Optional[int] = ...) -> None: ...

class Capabilities(_message.Message):
    __slots__ = ("ver", "arch", "dig", "ana", "feat")
    VER_FIELD_NUMBER: _ClassVar[int]
    ARCH_FIELD_NUMBER: _ClassVar[int]
    DIG_FIELD_NUMBER: _ClassVar[int]
    ANA_FIELD_NUMBER: _ClassVar[int]
    FEAT_FIELD_NUMBER: _ClassVar[int]
    ver: int
    arch: int
    dig: int
    ana: int
    feat: int
    def __init__(self, ver: _Optional[int] = ..., arch: _Optional[int] = ..., dig: _Optional[int] = ..., ana: _Optional[int] = ..., feat: _Optional[int] = ...) -> None: ...

class PinMode(_message.Message):
    __slots__ = ("pin", "mode")
    PIN_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    pin: int
    mode: int
    def __init__(self, pin: _Optional[int] = ..., mode: _Optional[int] = ...) -> None: ...

class DigitalWrite(_message.Message):
    __slots__ = ("pin", "value")
    PIN_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    pin: int
    value: int
    def __init__(self, pin: _Optional[int] = ..., value: _Optional[int] = ...) -> None: ...

class AnalogWrite(_message.Message):
    __slots__ = ("pin", "value")
    PIN_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    pin: int
    value: int
    def __init__(self, pin: _Optional[int] = ..., value: _Optional[int] = ...) -> None: ...

class PinRead(_message.Message):
    __slots__ = ("pin",)
    PIN_FIELD_NUMBER: _ClassVar[int]
    pin: int
    def __init__(self, pin: _Optional[int] = ...) -> None: ...

class DigitalReadResponse(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: int
    def __init__(self, value: _Optional[int] = ...) -> None: ...

class AnalogReadResponse(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: int
    def __init__(self, value: _Optional[int] = ...) -> None: ...

class ConsoleWrite(_message.Message):
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...

class DatastorePut(_message.Message):
    __slots__ = ("key", "value")
    KEY_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    key: str
    value: bytes
    def __init__(self, key: _Optional[str] = ..., value: _Optional[bytes] = ...) -> None: ...

class DatastoreGet(_message.Message):
    __slots__ = ("key",)
    KEY_FIELD_NUMBER: _ClassVar[int]
    key: str
    def __init__(self, key: _Optional[str] = ...) -> None: ...

class DatastoreGetResponse(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: bytes
    def __init__(self, value: _Optional[bytes] = ...) -> None: ...

class MailboxPush(_message.Message):
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...

class MailboxProcessed(_message.Message):
    __slots__ = ("message_id",)
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    message_id: int
    def __init__(self, message_id: _Optional[int] = ...) -> None: ...

class MailboxAvailableResponse(_message.Message):
    __slots__ = ("count",)
    COUNT_FIELD_NUMBER: _ClassVar[int]
    count: int
    def __init__(self, count: _Optional[int] = ...) -> None: ...

class MailboxReadResponse(_message.Message):
    __slots__ = ("content",)
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    content: bytes
    def __init__(self, content: _Optional[bytes] = ...) -> None: ...

class FileWrite(_message.Message):
    __slots__ = ("path", "data")
    PATH_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    path: str
    data: bytes
    def __init__(self, path: _Optional[str] = ..., data: _Optional[bytes] = ...) -> None: ...

class FileRead(_message.Message):
    __slots__ = ("path",)
    PATH_FIELD_NUMBER: _ClassVar[int]
    path: str
    def __init__(self, path: _Optional[str] = ...) -> None: ...

class FileRemove(_message.Message):
    __slots__ = ("path",)
    PATH_FIELD_NUMBER: _ClassVar[int]
    path: str
    def __init__(self, path: _Optional[str] = ...) -> None: ...

class FileReadResponse(_message.Message):
    __slots__ = ("content",)
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    content: bytes
    def __init__(self, content: _Optional[bytes] = ...) -> None: ...

class ProcessRunAsync(_message.Message):
    __slots__ = ("command",)
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    command: str
    def __init__(self, command: _Optional[str] = ...) -> None: ...

class ProcessRunAsyncResponse(_message.Message):
    __slots__ = ("pid",)
    PID_FIELD_NUMBER: _ClassVar[int]
    pid: int
    def __init__(self, pid: _Optional[int] = ...) -> None: ...

class ProcessPoll(_message.Message):
    __slots__ = ("pid",)
    PID_FIELD_NUMBER: _ClassVar[int]
    pid: int
    def __init__(self, pid: _Optional[int] = ...) -> None: ...

class ProcessPollResponse(_message.Message):
    __slots__ = ("status", "exit_code", "stdout_data", "stderr_data", "finished", "stdout_truncated", "stderr_truncated")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    STDOUT_DATA_FIELD_NUMBER: _ClassVar[int]
    STDERR_DATA_FIELD_NUMBER: _ClassVar[int]
    FINISHED_FIELD_NUMBER: _ClassVar[int]
    STDOUT_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    STDERR_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    status: int
    exit_code: int
    stdout_data: bytes
    stderr_data: bytes
    finished: bool
    stdout_truncated: bool
    stderr_truncated: bool
    def __init__(self, status: _Optional[int] = ..., exit_code: _Optional[int] = ..., stdout_data: _Optional[bytes] = ..., stderr_data: _Optional[bytes] = ..., finished: bool = ..., stdout_truncated: bool = ..., stderr_truncated: bool = ...) -> None: ...

class ProcessKill(_message.Message):
    __slots__ = ("pid",)
    PID_FIELD_NUMBER: _ClassVar[int]
    pid: int
    def __init__(self, pid: _Optional[int] = ...) -> None: ...

class GenericResponse(_message.Message):
    __slots__ = ("status", "message")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    status: str
    message: str
    def __init__(self, status: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class StructuredEntry(_message.Message):
    __slots__ = ("key", "string_value", "bytes_value", "bool_value", "int_value", "double_value", "null_value")
    KEY_FIELD_NUMBER: _ClassVar[int]
    STRING_VALUE_FIELD_NUMBER: _ClassVar[int]
    BYTES_VALUE_FIELD_NUMBER: _ClassVar[int]
    BOOL_VALUE_FIELD_NUMBER: _ClassVar[int]
    INT_VALUE_FIELD_NUMBER: _ClassVar[int]
    DOUBLE_VALUE_FIELD_NUMBER: _ClassVar[int]
    NULL_VALUE_FIELD_NUMBER: _ClassVar[int]
    key: str
    string_value: str
    bytes_value: bytes
    bool_value: bool
    int_value: int
    double_value: float
    null_value: bool
    def __init__(self, key: _Optional[str] = ..., string_value: _Optional[str] = ..., bytes_value: _Optional[bytes] = ..., bool_value: bool = ..., int_value: _Optional[int] = ..., double_value: _Optional[float] = ..., null_value: bool = ...) -> None: ...

class StructuredPayload(_message.Message):
    __slots__ = ("entries",)
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[StructuredEntry]
    def __init__(self, entries: _Optional[_Iterable[_Union[StructuredEntry, _Mapping[str, object]]]] = ...) -> None: ...

class AckPacket(_message.Message):
    __slots__ = ("command_id",)
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    command_id: int
    def __init__(self, command_id: _Optional[int] = ...) -> None: ...

class HandshakeConfig(_message.Message):
    __slots__ = ("ack_timeout_ms", "ack_retry_limit", "response_timeout_ms")
    ACK_TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    ACK_RETRY_LIMIT_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    ack_timeout_ms: int
    ack_retry_limit: int
    response_timeout_ms: int
    def __init__(self, ack_timeout_ms: _Optional[int] = ..., ack_retry_limit: _Optional[int] = ..., response_timeout_ms: _Optional[int] = ...) -> None: ...

class SetBaudratePacket(_message.Message):
    __slots__ = ("baudrate",)
    BAUDRATE_FIELD_NUMBER: _ClassVar[int]
    baudrate: int
    def __init__(self, baudrate: _Optional[int] = ...) -> None: ...

class LinkSync(_message.Message):
    __slots__ = ("nonce", "tag")
    NONCE_FIELD_NUMBER: _ClassVar[int]
    TAG_FIELD_NUMBER: _ClassVar[int]
    nonce: bytes
    tag: bytes
    def __init__(self, nonce: _Optional[bytes] = ..., tag: _Optional[bytes] = ...) -> None: ...

class EnterBootloader(_message.Message):
    __slots__ = ("magic",)
    MAGIC_FIELD_NUMBER: _ClassVar[int]
    magic: int
    def __init__(self, magic: _Optional[int] = ...) -> None: ...

class SpiTransfer(_message.Message):
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...

class SpiTransferResponse(_message.Message):
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...

class SpiConfig(_message.Message):
    __slots__ = ("bit_order", "data_mode", "frequency")
    BIT_ORDER_FIELD_NUMBER: _ClassVar[int]
    DATA_MODE_FIELD_NUMBER: _ClassVar[int]
    FREQUENCY_FIELD_NUMBER: _ClassVar[int]
    bit_order: int
    data_mode: int
    frequency: int
    def __init__(self, bit_order: _Optional[int] = ..., data_mode: _Optional[int] = ..., frequency: _Optional[int] = ...) -> None: ...
