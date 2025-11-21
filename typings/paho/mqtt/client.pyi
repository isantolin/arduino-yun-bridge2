from __future__ import annotations

from typing import Any


class Client:
    def __init__(self, *args: Any, **kwargs: Any) -> None: ...

    def connect(self, host: str, port: int, keepalive: int = ...) -> int: ...

    def disconnect(self) -> int: ...

    def loop_start(self) -> None: ...

    def loop_stop(self) -> None: ...

    def publish(self, *args: Any, **kwargs: Any) -> Any: ...

    def username_pw_set(
        self,
        username: str | None,
        password: str | None = ...,
    ) -> None: ...


class Properties:
    def __init__(self, prop_type: int) -> None: ...

    session_expiry_interval: int
    request_response_information: int
    request_problem_information: int


class MQTTMessage:
    topic: str | bytes | None
    payload: bytes
    qos: int
    retain: bool


MQTT_CLEAN_START_FIRST_ONLY: int
CONNACK_REFUSED_BAD_USERNAME_PASSWORD: int
CONNACK_REFUSED_NOT_AUTHORIZED: int
CONNACK_REFUSED_SERVER_UNAVAILABLE: int

__all__ = [
    "Client",
    "Properties",
    "MQTTMessage",
    "MQTT_CLEAN_START_FIRST_ONLY",
    "CONNACK_REFUSED_BAD_USERNAME_PASSWORD",
    "CONNACK_REFUSED_NOT_AUTHORIZED",
    "CONNACK_REFUSED_SERVER_UNAVAILABLE",
]
