"""Manual stub for paho.mqtt.properties."""

from __future__ import annotations

from typing import Any

class Properties:
    """MQTT v5 properties container with attribute-style access."""

    packetType: int
    types: list[Any]
    names: dict[str, Any]
    properties: dict[str, Any]

    def __init__(self, packetType: int) -> None: ...

    # --- PUBLISH properties ---
    ContentType: str
    PayloadFormatIndicator: int
    MessageExpiryInterval: int
    ResponseTopic: str
    CorrelationData: bytes
    UserProperty: list[tuple[str, str]]
    SubscriptionIdentifier: list[int]
    TopicAlias: int

    # --- CONNECT properties ---
    SessionExpiryInterval: int
    RequestResponseInformation: int
    RequestProblemInformation: int
    TopicAliasMaximum: int

    # --- CONNACK / general properties ---
    AssignedClientIdentifier: str
    ServerKeepAlive: int
    AuthenticationMethod: str
    AuthenticationData: bytes
    WillDelayInterval: int
    ResponseInformation: str
    ServerReference: str
    ReasonString: str
    ReceiveMaximum: int
    MaximumQoS: int
    RetainAvailable: int
    MaximumPacketSize: int
    WildcardSubscriptionAvailable: int
    SubscriptionIdentifierAvailable: int
    SharedSubscriptionAvailable: int

    def allowsMultiple(self, compressedName: str) -> bool: ...
    def clear(self) -> None: ...
    def getIdentFromName(self, compressedName: str) -> int: ...
    def getNameFromIdent(self, identifier: int) -> str: ...
    def isEmpty(self) -> bool: ...
    def json(self) -> dict[str, Any]: ...
    def pack(self) -> bytes: ...
    def readProperty(
        self, propsname: str, propsbuf: bytes, *, result: Any = ...
    ) -> tuple[Any, int]: ...
    def unpack(self, buffer: bytes) -> tuple[Properties, int]: ...
    def writeProperty(self, identifier: int, type_: int, value: Any) -> bytes: ...
