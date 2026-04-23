"""Manual stub for paho.mqtt.publish."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, Required, TypedDict

from paho.mqtt.enums import MQTTProtocolVersion
from paho.mqtt.properties import Properties as Properties

PayloadType = str | bytes | bytearray | int | float | None

class AuthParameter(TypedDict, total=False):
    username: Required[str]
    password: NotRequired[str]

class TLSParameter(TypedDict, total=False):
    ca_certs: Required[str]
    certfile: NotRequired[str]
    keyfile: NotRequired[str]
    tls_version: NotRequired[int]
    ciphers: NotRequired[str]
    insecure: NotRequired[bool]

class MessageDict(TypedDict, total=False):
    topic: Required[str]
    payload: NotRequired[PayloadType]
    qos: NotRequired[int]
    retain: NotRequired[bool]

def single(
    topic: str,
    payload: PayloadType = None,
    qos: int = 0,
    retain: bool = False,
    hostname: str = "localhost",
    port: int = 1883,
    client_id: str = "",
    keepalive: int = 60,
    will: MessageDict | None = None,
    auth: AuthParameter | None = None,
    tls: TLSParameter | None = None,
    protocol: MQTTProtocolVersion = ...,
    transport: Literal["tcp", "websockets"] = "tcp",
    proxy_args: Any | None = None,
) -> None: ...
def multiple(
    msgs: list[MessageDict | tuple[str, PayloadType, int, bool]],
    hostname: str = "localhost",
    port: int = 1883,
    client_id: str = "",
    keepalive: int = 60,
    will: MessageDict | None = None,
    auth: AuthParameter | None = None,
    tls: TLSParameter | None = None,
    protocol: MQTTProtocolVersion = ...,
    transport: Literal["tcp", "websockets"] = "tcp",
    proxy_args: Any | None = None,
) -> None: ...
