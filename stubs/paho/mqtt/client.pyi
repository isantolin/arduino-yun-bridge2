from typing import Any, Callable, Optional, Union
from .properties import Properties

MQTTv31: int
MQTTv311: int
MQTTv5: int

class MQTTMessageInfo:
    mid: int
    rc: int
    def is_published(self) -> bool: ...
    def wait_for_publish(self, timeout: float = ...) -> None: ...

class Client:
    def __init__(
        self,
        client_id: str = "",
        clean_session: bool | None = None,
        userdata: Any = None,
        protocol: int = MQTTv311,
        transport: str = "tcp",
        reconnect_on_failure: bool = True,
    ) -> None: ...
    
    def tls_set(
        self,
        ca_certs: str | None = None,
        certfile: str | None = None,
        keyfile: str | None = None,
        cert_reqs: int | None = None,
        tls_version: int | None = None,
        ciphers: str | None = None,
        keyfile_password: str | None = None,
    ) -> None: ...
    
    def username_pw_set(
        self,
        username: str | None = None,
        password: str | None = None,
    ) -> None: ...
    
    def connect(
        self,
        host: str,
        port: int = 1883,
        keepalive: int = 60,
        bind_address: str = "",
        bind_port: int = 0,
        clean_start: int = 3,
        properties: Properties | None = None,
    ) -> int: ...
    
    def loop_start(self) -> None: ...
    def loop_stop(self, force: bool = False) -> None: ...
    
    def publish(
        self,
        topic: str,
        payload: Any = None,
        qos: int = 0,
        retain: bool = False,
        properties: Properties | None = None,
    ) -> MQTTMessageInfo: ...
    
    def disconnect(
        self,
        reasoncode: int | None = None,
        properties: Properties | None = None,
    ) -> int: ...
