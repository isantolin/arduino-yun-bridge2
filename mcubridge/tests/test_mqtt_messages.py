"""Verify that QueuedPublish correctly roundtrips via direct JSON serialization."""

from __future__ import annotations

import msgspec

from mcubridge.protocol.structures import QueuedPublish
from mcubridge.protocol import protocol


def test_queued_publish_json_roundtrip() -> None:

    message = QueuedPublish(
        topic_name=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test",
        payload=b"hello",
        qos=1,
        retain=True,
        content_type="application/octet-stream",
        payload_format_indicator=1,
        message_expiry_interval=30,
        response_topic=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/resp",
        correlation_data=b"cid",
        user_properties=(("k", "v"),),
    )

    # Use direct library calls as per zero-wrapper mandate
    encoded = msgspec.json.encode(message)
    restored = msgspec.json.decode(encoded, type=QueuedPublish)

    assert restored == message
    assert restored.correlation_data == b"cid"
    assert restored.user_properties == (("k", "v"),)
