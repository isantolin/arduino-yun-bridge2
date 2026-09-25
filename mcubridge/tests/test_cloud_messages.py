from __future__ import annotations

from hypothesis import given, settings, strategies as st

from mcubridge.protocol.structures import (
    create_queued_publish,
)
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol


@settings(max_examples=50, derandomize=True, deadline=None)
@given(
    topic_suffix=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=64),
    payload=st.binary(min_size=0, max_size=2048),
    content_type=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=32),
    expiry=st.integers(min_value=0, max_value=2**32 - 1),
    qos=st.integers(min_value=0, max_value=2),
    retain=st.booleans(),
    payload_format=st.integers(min_value=0, max_value=1),
    response_suffix=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=64),
    correlation_data=st.binary(max_size=64),
    user_properties=st.lists(
        st.tuples(
            st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=32),
            st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=64),
        ),
        max_size=6,
    ),
)
def test_queued_publish_protobuf_roundtrip(
    topic_suffix: str,
    payload: bytes,
    content_type: str,
    expiry: int,
    qos: int,
    retain: bool,
    payload_format: int,
    response_suffix: str,
    correlation_data: bytes,
    user_properties: list[tuple[str, str]],
) -> None:
    """Verify that CloudQueuedPublish correctly roundtrips via protobuf serialization for any valid inputs."""
    topic_name = f"{protocol.CLOUD_DEFAULT_TOPIC_PREFIX}/{topic_suffix}"
    response_topic = f"{protocol.CLOUD_DEFAULT_TOPIC_PREFIX}/{response_suffix}" if response_suffix else ""
    message = create_queued_publish(
        topic_name=topic_name,
        payload=payload,
        content_type=content_type,
        message_expiry_interval=expiry,
        user_properties=tuple(user_properties),
    )
    message.qos = qos
    message.retain = retain
    message.payload_format_indicator = payload_format
    if response_topic:
        message.response_topic = response_topic
    if correlation_data:
        message.correlation_data = correlation_data

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

    restored_props = [(p.key, p.value) for p in restored.user_properties]
    assert restored_props == user_properties
