from __future__ import annotations

from hypothesis import example, given
from mcubridge.protocol import cloud_gateway_pb2 as pb

from tests.conftest import st_cloud_queued_publish


@given(message=st_cloud_queued_publish())
@example(message=pb.CloudQueuedPublish(topic_name="br/test", payload=b""))
@example(message=pb.CloudQueuedPublish(topic_name="br/telemetry", payload=b"\x00\xff" * 128))
def test_queued_publish_protobuf_roundtrip(message: pb.CloudQueuedPublish) -> None:
    """Verify that CloudQueuedPublish correctly roundtrips via protobuf serialization for any valid inputs."""
    encoded = message.SerializeToString()
    restored = pb.CloudQueuedPublish.FromString(encoded)

    assert restored.topic_name == message.topic_name
    assert restored.payload == message.payload
