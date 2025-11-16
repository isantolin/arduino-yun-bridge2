from __future__ import annotations


class MqttError(Exception):
    ...


class MqttConnectError(MqttError):
    rc: int

    def __init__(self, rc: int = ..., *args: object) -> None: ...
