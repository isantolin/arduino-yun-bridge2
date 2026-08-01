"""Exhaustive tests for mcubridge.protocol.frame and mcubridge.protocol.structures. [SIL-2]"""

from __future__ import annotations

import pytest
from mcubridge.protocol import frame, mcubridge_pb2 as pb, protocol, structures
from typing import Any

# =============================================================================
# 1. Tests for mcubridge.protocol.frame
# =============================================================================


def test_build_frame_invalid_command_id() -> None:
    with pytest.raises(ValueError, match="Invalid command ID"):
        frame.build_frame(-1, 1)

    with pytest.raises(ValueError, match="Invalid command ID"):
        frame.build_frame(0x10000, 1)


def test_build_frame_unencrypted_bytes() -> None:
    raw = frame.build_frame(command_id=0x01, sequence_id=10, payload=b"hello_world")
    decoded = frame.parse_frame(raw)
    assert decoded.envelope.command_id == 0x01
    assert decoded.envelope.sequence_id == 10
    assert decoded.payload == b"hello_world"


def test_build_frame_unencrypted_protobuf_message() -> None:
    req = pb.ConsoleWrite(data=b"hello")
    raw = frame.build_frame(command_id=0x02, sequence_id=11, payload=req)
    decoded = frame.parse_frame(raw)
    assert decoded.envelope.command_id == 0x02
    assert decoded.envelope.sequence_id == 11
    assert isinstance(decoded.payload, pb.ConsoleWrite)
    assert decoded.payload.data == b"hello"


def test_build_frame_unencrypted_bytes_exceeds_max_payload() -> None:
    too_large = b"X" * (protocol.MAX_PAYLOAD_SIZE + 1)
    with pytest.raises(ValueError, match="exceeds maximum"):
        frame.build_frame(command_id=0x01, sequence_id=1, payload=too_large)


def test_build_frame_encrypted_protobuf_message_exceeds_max_payload() -> None:
    key = b"\x00" * 32
    too_large = b"Y" * (protocol.MAX_PAYLOAD_SIZE + 1)
    with pytest.raises(ValueError, match="exceeds maximum"):
        frame.build_frame(command_id=0x10, sequence_id=1, payload=too_large, session_key=key)


def test_build_frame_encrypted_success() -> None:
    key = b"\x01" * 32
    nonce = b"\x02" * 12
    raw = frame.build_frame(
        command_id=0x10,
        sequence_id=20,
        payload=b"secret_data",
        nonce=nonce,
        session_key=key,
    )
    decoded = frame.parse_frame(raw, session_key=key)
    assert decoded.envelope.command_id == 0x10
    assert decoded.envelope.sequence_id == 20
    assert decoded.payload == b"secret_data"


def test_build_frame_encrypted_with_protobuf_payload() -> None:
    key = b"\x03" * 32
    nonce = b"\x04" * 12
    req = pb.PinControlRequest(state="OFF")
    raw = frame.build_frame(
        command_id=0x10,
        sequence_id=21,
        payload=req,
        nonce=nonce,
        session_key=key,
    )
    decoded = frame.parse_frame(raw, session_key=key)
    assert decoded.payload == req.SerializeToString()


def test_parse_frame_incomplete() -> None:
    with pytest.raises(ValueError, match="Incomplete frame: too short"):
        frame.parse_frame(b"12")


def test_parse_frame_crc_mismatch() -> None:
    raw = frame.build_frame(command_id=0x01, sequence_id=1, payload=b"test")
    corrupt = raw[:-1] + b"\xff"
    with pytest.raises(ValueError, match="CRC mismatch"):
        frame.parse_frame(corrupt)


def test_parse_frame_protobuf_decode_error() -> None:
    # Payload with invalid protobuf byte stream before CRC
    corrupt_body = b"\xff\xff\xff\xff"
    from binascii import crc32

    crc_bytes = (crc32(corrupt_body) & protocol.CRC32_MASK).to_bytes(4, "little")
    raw = corrupt_body + crc_bytes
    with pytest.raises(ValueError, match="Failed to parse Protobuf envelope"):
        frame.parse_frame(raw)


def test_parse_frame_invalid_version() -> None:
    env = pb.RpcEnvelope(version=999, command_id=1, sequence_id=1)
    body = env.SerializeToString()
    from binascii import crc32

    crc_bytes = (crc32(body) & protocol.CRC32_MASK).to_bytes(4, "little")
    raw = body + crc_bytes
    with pytest.raises(ValueError, match="Invalid protocol version"):
        frame.parse_frame(raw)


def test_parse_frame_aead_decryption_failed() -> None:
    key = b"\x05" * 32
    wrong_key = b"\x06" * 32
    raw = frame.build_frame(command_id=0x10, sequence_id=1, payload=b"secret", session_key=key)
    with pytest.raises(ValueError, match="AEAD decryption failed"):
        frame.parse_frame(raw, session_key=wrong_key)


def test_parse_frame_empty_oneof_payload() -> None:
    env = pb.RpcEnvelope(version=protocol.PROTOCOL_VERSION, command_id=1, sequence_id=1)
    body = env.SerializeToString()
    from binascii import crc32

    crc_bytes = (crc32(body) & protocol.CRC32_MASK).to_bytes(4, "little")
    raw = body + crc_bytes
    decoded = frame.parse_frame(raw)
    assert decoded.payload == b""


# =============================================================================
# 2. Tests for mcubridge.protocol.structures
# =============================================================================


def test_topic_route_properties() -> None:
    route = structures.TopicRoute(
        raw="prefix/file/read",
        prefix="prefix",
        topic="file",
        segments=("read", "subpath"),
    )
    assert route.identifier == "read"
    assert route.remainder == ("subpath",)
    assert route.action == "read"

    route_empty = structures.TopicRoute(raw="prefix", prefix="prefix", topic="", segments=())
    assert route_empty.identifier == ""
    assert route_empty.remainder == ()
    assert route_empty.action is None

    route_resp = structures.TopicRoute(raw="prefix/d/response", prefix="prefix", topic="d", segments=("response",))
    assert route_resp.action is None


def test_is_command_allowed() -> None:
    policy = pb.AllowedCommandPolicy(entries=["reboot", "ls", "*"])
    assert structures.is_command_allowed(policy, "reboot now") is True
    assert structures.is_command_allowed(policy, "") is False

    specific_policy = pb.AllowedCommandPolicy(entries=["cat", "ls"])
    assert structures.is_command_allowed(specific_policy, "ls -la") is True
    assert structures.is_command_allowed(specific_policy, "rm -rf /") is False


def test_create_allowed_policy() -> None:
    policy_wildcard = structures.create_allowed_policy(["ls", "*", "cat"])
    assert policy_wildcard.entries == ["*"]

    policy_sorted = structures.create_allowed_policy(["cat, ls", "grep"])
    assert policy_sorted.entries == ["cat", "grep", "ls"]


def test_allows_topic() -> None:
    auth = pb.TopicAuthorization(digital_read=True, digital_write=False)
    assert structures.allows_topic(auth, "d", "read") is True
    assert structures.allows_topic(auth, "d", "write") is False
    assert structures.allows_topic(auth, "unknown", "action") is False


def _valid_runtime_config(**overrides: Any) -> pb.RuntimeConfig:
    """Build a RuntimeConfig satisfying all buf.validate rules, with overrides for testing."""
    base: dict[str, Any] = dict(
        serial_port="/dev/ttyATH0",
        cloud_port=8883,
        topic_prefix="bridge",
        watchdog_enabled=True,
        watchdog_interval=1.0,
        serial_shared_secret=b"secret",
        metrics_port=9130,
        cloud_http3_port=443,
        allow_non_tmp_paths=False,
        cloud_spool_dir="/tmp/spool",
        file_system_root="/tmp",
        status_interval=60,
    )
    base.update(overrides)
    return pb.RuntimeConfig(**base)


def test_validate_config_invalid() -> None:
    with pytest.raises(ValueError, match="topic_prefix"):
        structures.validate_config(_valid_runtime_config(topic_prefix=""))

    with pytest.raises(ValueError, match="watchdog_interval must be >= 0.5s"):
        structures.validate_config(_valid_runtime_config(watchdog_interval=0.1))

    # Disabling the watchdog lifts the CEL constraint on watchdog_interval.
    structures.validate_config(_valid_runtime_config(watchdog_enabled=False, watchdog_interval=0.1))

    with pytest.raises(ValueError, match="cloud_spool_dir"):
        structures.validate_config(_valid_runtime_config(cloud_spool_dir="/invalid/flash/path"))

    with pytest.raises(ValueError, match="file_system_root"):
        structures.validate_config(_valid_runtime_config(file_system_root="/invalid/root"))


def test_get_ssl_context() -> None:
    cfg = pb.RuntimeConfig(cloud_tls=False)
    assert structures.get_ssl_context(cfg) is None

    cfg_tls = pb.RuntimeConfig(
        cloud_tls=True,
        cloud_cafile="/nonexistent/ca.pem",
    )
    with pytest.raises(RuntimeError, match="Cloud TLS CA file missing|TLS setup failed"):
        structures.get_ssl_context(cfg_tls)

    cfg_mtls_invalid = pb.RuntimeConfig(
        cloud_tls=True,
        cloud_certfile="/path/to/cert",
        cloud_keyfile="",
    )
    with pytest.raises(RuntimeError, match="Both cloud_certfile and cloud_keyfile|TLS setup failed"):
        structures.get_ssl_context(cfg_mtls_invalid)


def test_replace_cloud_publish() -> None:
    original = pb.CloudQueuedPublish(
        topic_name="old_topic",
        payload=b"old_payload",
        qos=1,
    )
    replaced = structures.replace_cloud_publish(
        original,
        topic_name="new_topic",
        user_properties=[("key1", "val1")],
        subscription_identifier=[100],
    )
    assert replaced.topic_name == "new_topic"
    assert len(replaced.user_properties) == 1
    assert replaced.user_properties[0].key == "key1"
    assert list(replaced.subscription_identifier) == [100]


def test_resolve_cloud_context() -> None:
    msg = pb.CloudQueuedPublish(topic_name="initial", payload=b"p")

    # context is None
    assert structures.resolve_cloud_context(msg, None) == msg

    # context with properties
    class FakeProps:
        ResponseTopic = "reply/topic"
        CorrelationData = b"\x01\x02"

    class FakeContext:
        properties = FakeProps()
        topic = "req/topic"

    resolved = structures.resolve_cloud_context(msg, FakeContext())
    assert resolved.topic_name == "reply/topic"
    assert resolved.correlation_data == b"\x01\x02"
    assert any(p.key == "bridge-request-topic" and p.value == "req/topic" for p in resolved.user_properties)


def test_pending_command_methods() -> None:
    cmd = structures.PendingCommand(command_id=1)
    assert cmd.completion.is_set() is False

    cmd.mark_success(b"response")
    assert cmd.success is True
    assert cmd.response_payload == b"response"
    assert cmd.completion.is_set() is True

    cmd2 = structures.PendingCommand(command_id=2)
    cmd2.mark_failure(status=404)
    assert cmd2.success is False
    assert cmd2.failure_status == 404
    assert cmd2.completion.is_set() is True
