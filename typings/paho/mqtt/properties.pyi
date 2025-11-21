from __future__ import annotations

from typing import List, Tuple


class Properties:
    def __init__(self, packet_type: int) -> None: ...

    content_type: str | None
    payload_format_indicator: int | None
    message_expiry_interval: int | None
    response_topic: str | None
    correlation_data: bytes | None
    user_property: List[Tuple[str, str]]
    session_expiry_interval: int | None
    request_response_information: int | None
    request_problem_information: int | None


__all__ = ["Properties"]
