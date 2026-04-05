"""Manual stub for paho.mqtt.client."""

from __future__ import annotations

from typing import Any, Callable

from paho.mqtt.enums import CallbackAPIVersion, MQTTProtocolVersion
from paho.mqtt.properties import Properties

MQTTv5: int
MQTT_ERR_SUCCESS: int

class MQTTMessage:
    topic: str
    payload: bytes
    qos: int
    retain: bool
    mid: int
    properties: Properties | None
    timestamp: float

class Client:
    def __init__(
        self,
        client_id: str = "",
        clean_session: bool | None = None,
        userdata: Any = None,
        protocol: int | MQTTProtocolVersion = ...,
        transport: str = "tcp",
        reconnect_on_failure: bool = True,
        callback_api_version: CallbackAPIVersion | int = ...,
    ) -> None: ...
    def username_pw_set(
        self, username: str, password: str | None = None
    ) -> None: ...
    def tls_set(
        self,
        ca_certs: str | None = None,
        certfile: str | None = None,
        keyfile: str | None = None,
        cert_reqs: Any = None,
        tls_version: Any = None,
        ciphers: str | None = None,
    ) -> None: ...
    def tls_insecure_set(self, value: bool) -> None: ...
    def connect(
        self,
        host: str,
        port: int = 1883,
        keepalive: int = 60,
        bind_address: str = "",
        bind_port: int = 0,
        clean_start: int = ...,
        properties: Properties | None = None,
    ) -> int: ...
    def disconnect(
        self, reasoncode: Any = None, properties: Properties | None = None
    ) -> int: ...
    def subscribe(
        self, topic: str | list[tuple[str, int]], qos: int = 0, **kwargs: Any
    ) -> tuple[int, int]: ...
    def publish(
        self,
        topic: str,
        payload: str | bytes | bytearray | int | float | None = None,
        qos: int = 0,
        retain: bool = False,
        properties: Properties | None = None,
    ) -> Any: ...
    def loop_start(self) -> int: ...
    def loop_stop(self) -> int: ...
    def loop_forever(self, **kwargs: Any) -> int: ...
    def on_connect(self, *args: Any, **kwargs: Any) -> Any: ...
    def on_message(self, *args: Any, **kwargs: Any) -> Any: ...
    def on_disconnect(self, *args: Any, **kwargs: Any) -> Any: ...
    @property
    def is_connected(self) -> bool: ...
    _userdata: Any
