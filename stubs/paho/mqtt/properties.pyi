from typing import Any
from .packettypes import PacketTypes

class Properties:
    def __init__(self, packetType: PacketTypes) -> None: ...
    ContentType: str | None
    PayloadFormatIndicator: int | None
    MessageExpiryInterval: int | None
    ResponseTopic: str | None
    CorrelationData: bytes | None
    UserProperty: list[tuple[str, str]] | None
    SessionExpiryInterval: int | None
    RequestResponseInformation: int | None
    RequestProblemInformation: int | None
