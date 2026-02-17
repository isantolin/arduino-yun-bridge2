from __future__ import annotations

from mcubridge.mqtt.messages import QueuedPublish, SpoolRecord
from mcubridge.protocol import protocol


def test_queued_publish_roundtrip_with_correlation_and_user_properties() -> None:
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

    record = message.to_record()
    restored = QueuedPublish.from_record(record)

    assert restored.topic_name == message.topic_name
    assert restored.payload == message.payload
    assert restored.qos == message.qos
    assert restored.retain == message.retain
    assert restored.correlation_data == b"cid"
    assert restored.user_properties == (("k", "v"),)


def test_queued_publish_from_record_normalizes_user_properties() -> None:
    record: SpoolRecord = {
        "topic_name": f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test",
        "payload": "aGVsbG8=",  # base64("hello")
        "user_properties": [
            ["k", "v"],
            ["only-one"],
            "not-a-seq",
            [1, 2, 3],
        ],
    }

    restored = QueuedPublish.from_record(record)
    assert restored.user_properties == (("k", "v"), ("1", "2"))


def test_queued_publish_from_record_handles_missing_correlation_data() -> None:
    record: SpoolRecord = {
        "topic_name": f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/test",
        "payload": "",  # empty
        "correlation_data": None,
    }

    restored = QueuedPublish.from_record(record)
    assert restored.payload == b""
    assert restored.correlation_data is None
