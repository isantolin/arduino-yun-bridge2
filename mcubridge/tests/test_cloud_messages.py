from __future__ import annotations

from hypothesis import example, given
from mcubridge.protocol import mcubridge_pb2 as pb

from tests.conftest import st_cloud_queued_publish


@given(message=st_cloud_queued_publish())
@example(message=pb.CloudQueuedPublish(topic_name="br/test", payload=b""))
@example(message=pb.CloudQueuedPublish(topic_name="br/telemetry", payload=b"\x00\xff" * 128, qos=2, retain=True))
def test_queued_publish_protobuf_roundtrip(message: pb.CloudQueuedPublish) -> None:
    """Verify that CloudQueuedPublish correctly roundtrips via protobuf serialization for any valid inputs."""
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
