from __future__ import annotations

from mcubridge.protocol.structures import (
    create_queued_publish,
)
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol


def test_queued_publish_protobuf_roundtrip() -> None:
    """Verify that CloudQueuedPublish correctly roundtrips via protobuf serialization."""
    message = create_queued_publish(
        topic_name=f"{protocol.CLOUD_DEFAULT_TOPIC_PREFIX}/test",
        payload=b"hello",
        content_type="application/octet-stream",
        message_expiry_interval=30,
        user_properties=(("k", "v"),),
    )
    message.qos = 1
    message.retain = True
    message.payload_format_indicator = 1
    message.response_topic = f"{protocol.CLOUD_DEFAULT_TOPIC_PREFIX}/resp"
    message.correlation_data = b"cid"

    # Direct Protobuf calls — Zero-Wrapper policy
    encoded = message.SerializeToString()
    restored = pb.CloudQueuedPublish.FromString(encoded)

    assert restored.topic_name == message.topic_name
    assert restored.payload == message.payload
    assert restored.qos == message.qos
    assert restored.retain == message.retain
    assert restored.content_type == message.content_type
    assert restored.payload_format_indicator == message.payload_format_indicator
    assert restored.message_expiry_interval == message.message_expiry_interval
    assert restored.response_topic == message.response_topic
    assert restored.correlation_data == message.correlation_data

    props = [(p.key, p.value) for p in restored.user_properties]
    assert props == [("k", "v")]
