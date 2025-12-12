from typing import Any

class Message:
    topic: Any
    payload: Any
    qos: int
    retain: bool
    mid: int
    properties: Any
