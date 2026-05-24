from mcubridge.protocol import protocol
from mcubridge.protocol.topics import parse_topic, topic_path
from mcubridge.protocol.protocol import Topic
from mcubridge.protocol.structures import McuCapabilities, FlowEvent, QueuedPublish, TopicAuthorization

# Original constants needed by other tests
TEST_CMD_ID = 0x42
TEST_SEQ_ID = 0x1234
TEST_PAYLOAD = b"Hello, Protobuf!"
TEST_NONCE = b"A" * 12
TEST_TAG = b"T" * 16
TEST_BROKEN_CRC = 0xDEADBEEF
TEST_RANDOM_SEED = 42


def test_constants_completeness():
    assert protocol.PROTOCOL_VERSION == 2


def test_topics_edge_cases():
    # 1. Invalid topic segment (ValueError)
    assert parse_topic("br", "br/invalid_topic/foo") is None

    # 2. Empty inputs
    assert parse_topic("", "foo") is None
    assert parse_topic("br", "") is None
    assert topic_path("", Topic.SYSTEM) == "system"
    assert topic_path("", Topic.SYSTEM, "") == "system"


def test_structures_coverage_boost():
    # Cover McuCapabilities properties
    assert "AVR" in McuCapabilities(board_arch=1).arch_name
    assert "ESP32" in McuCapabilities(board_arch=2).arch_name
    assert "ESP8266" in McuCapabilities(board_arch=3).arch_name
    assert "SAMD" in McuCapabilities(board_arch=4).arch_name
    assert "SAM" in McuCapabilities(board_arch=5).arch_name
    assert "Unknown" in McuCapabilities(board_arch=99).arch_name

    # Cover FlowEvent
    assert FlowEvent.SENT == "sent"

    # Cover QueuedPublish properties
    qp = QueuedPublish(topic_name="topic", payload=b"payload")
    assert qp.topic_name == "topic"
    assert qp.payload == b"payload"
    assert qp.qos == 0

    # Cover TopicAuthorization
    ta = TopicAuthorization()
    assert ta.file_read is True
    assert ta.file_write is True
