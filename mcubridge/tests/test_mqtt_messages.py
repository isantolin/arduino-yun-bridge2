from __future__ import annotations

from mcubridge.protocol.structures import QueuedPublish
from mcubridge.protocol import protocol


def test_queued_publish_protobuf_roundtrip() -> None:
    """Verify that QueuedPublish correctly roundtrips via protobuf serialization."""
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
    encoded = message.to_protobuf()
    restored = QueuedPublish.from_protobuf(encoded)

    assert restored == message
    assert restored.correlation_data == b"cid"
    assert restored.user_properties == (("k", "v"),)
