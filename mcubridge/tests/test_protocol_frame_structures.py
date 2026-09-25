"""Exhaustive tests for mcubridge.protocol.frame and mcubridge.protocol.structures. [SIL-2]"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st
import pytest

from mcubridge.protocol import frame, mcubridge_pb2 as pb, protocol, structures

# =============================================================================
# 1. Tests for mcubridge.protocol.frame
# =============================================================================


def test_build_frame_invalid_command_id() -> None:
    with pytest.raises(ValueError):
        frame.build_frame(-1, 1)

    with pytest.raises(ValueError):
        frame.build_frame(0x10000, 1)


@settings(max_examples=30, derandomize=True, deadline=None)
@given(
    cmd_id=st.integers(0, 0xFF),
    seq_id=st.integers(0, protocol.UINT16_MAX),
    payload=st.binary(min_size=0, max_size=protocol.MAX_PAYLOAD_SIZE),
)
def test_build_parse_frame_isomorphism_unencrypted_property(cmd_id: int, seq_id: int, payload: bytes) -> None:
    raw = frame.build_frame(command_id=cmd_id, sequence_id=seq_id, payload=payload)
    decoded = frame.parse_frame(raw)
    assert decoded.envelope.command_id == cmd_id
    assert decoded.envelope.sequence_id == seq_id
    assert decoded.payload == payload


def test_build_frame_unencrypted_protobuf_message() -> None:
    req = pb.ConsoleWrite(data=b"hello")
    raw = frame.build_frame(command_id=0x02, sequence_id=11, payload=req)
    decoded = frame.parse_frame(raw)
    assert decoded.envelope.command_id == 0x02
    assert decoded.envelope.sequence_id == 11
    assert isinstance(decoded.payload, pb.ConsoleWrite)
    assert decoded.payload.data == b"hello"


@settings(max_examples=25, derandomize=True, deadline=None)
@given(
    overflow_len=st.integers(protocol.MAX_PAYLOAD_SIZE + 1, protocol.MAX_PAYLOAD_SIZE + 64),
    encrypted=st.booleans(),
)
def test_build_frame_payload_overflow_property(overflow_len: int, encrypted: bool) -> None:
    key = b"\x00" * protocol.AEAD_KEY_SIZE if encrypted else None
    too_large = b"X" * overflow_len
    with pytest.raises(ValueError, match="exceeds maximum"):
        frame.build_frame(command_id=0x01, sequence_id=1, payload=too_large, session_key=key)


@settings(max_examples=30, derandomize=True, deadline=None)
@given(
    cmd_id=st.integers(0, 0xFF),
    seq_id=st.integers(0, protocol.UINT16_MAX),
    payload=st.binary(min_size=0, max_size=protocol.MAX_PAYLOAD_SIZE),
    key=st.binary(min_size=protocol.AEAD_KEY_SIZE, max_size=protocol.AEAD_KEY_SIZE),
    nonce=st.binary(min_size=protocol.AEAD_NONCE_SIZE, max_size=protocol.AEAD_NONCE_SIZE),
)
def test_build_parse_frame_isomorphism_encrypted_property(
    cmd_id: int, seq_id: int, payload: bytes, key: bytes, nonce: bytes
) -> None:
    raw = frame.build_frame(
        command_id=cmd_id,
        sequence_id=seq_id,
        payload=payload,
        nonce=nonce,
        session_key=key,
    )
    decoded = frame.parse_frame(raw, session_key=key)
    assert decoded.envelope.command_id == cmd_id
    assert decoded.envelope.sequence_id == seq_id
    assert decoded.payload == payload


def test_build_frame_encrypted_with_protobuf_payload() -> None:
    key = b"\x03" * protocol.AEAD_KEY_SIZE
    nonce = b"\x04" * protocol.AEAD_NONCE_SIZE
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

    crc_bytes = (crc32(corrupt_body) & protocol.CRC32_MASK).to_bytes(protocol.CRC_SIZE, "little")
    raw = corrupt_body + crc_bytes
    with pytest.raises(ValueError, match="Failed to parse Protobuf envelope"):
        frame.parse_frame(raw)


def test_parse_frame_invalid_version() -> None:
    env = pb.RpcEnvelope(version=999, command_id=1, sequence_id=1)
    body = env.SerializeToString()
    from binascii import crc32

    crc_bytes = (crc32(body) & protocol.CRC32_MASK).to_bytes(protocol.CRC_SIZE, "little")
    raw = body + crc_bytes
    with pytest.raises(ValueError, match="Unsupported protocol version"):
        frame.parse_frame(raw)


def test_parse_frame_aead_decryption_failed() -> None:
    key = b"\x05" * protocol.AEAD_KEY_SIZE
    wrong_key = b"\x06" * protocol.AEAD_KEY_SIZE
    raw = frame.build_frame(command_id=0x10, sequence_id=1, payload=b"secret", session_key=key)
    with pytest.raises(ValueError, match="AEAD decryption failed"):
        frame.parse_frame(raw, session_key=wrong_key)


def test_parse_frame_empty_oneof_payload() -> None:
    env = pb.RpcEnvelope(version=protocol.PROTOCOL_VERSION, command_id=1, sequence_id=1)
    body = env.SerializeToString()
    from binascii import crc32

    crc_bytes = (crc32(body) & protocol.CRC32_MASK).to_bytes(protocol.CRC_SIZE, "little")
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


@settings(max_examples=25, derandomize=True, deadline=None)
@given(
    cmd=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=16),
    arg=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=0, max_size=16),
)
def test_is_command_allowed_property(cmd: str, arg: str) -> None:
    policy = pb.AllowedCommandPolicy(entries=[cmd, "*"])
    full_cmd = f"{cmd} {arg}".strip()
    assert structures.is_command_allowed(policy, full_cmd) is True
    assert structures.is_command_allowed(policy, "") is False

    strict_policy = pb.AllowedCommandPolicy(entries=[cmd])
    assert structures.is_command_allowed(strict_policy, full_cmd) is True
    assert structures.is_command_allowed(strict_policy, f"disallowed_{cmd}") is False


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


def _valid_runtime_config() -> pb.RuntimeConfig:
    """Build a RuntimeConfig satisfying all validation rules."""
    cfg = pb.RuntimeConfig()
    cfg.serial_port = "/dev/ttyATH0"
    cfg.cloud_port = 8883
    cfg.topic_prefix = "bridge"
    cfg.watchdog_enabled = True
    cfg.watchdog_interval = 1.0
    cfg.serial_shared_secret = b"secret"
    cfg.cloud_http3_port = 443
    cfg.allow_non_tmp_paths = False
    cfg.cloud_spool_dir = "/tmp/spool"
    cfg.file_system_root = "/tmp"
    cfg.status_interval = 60
    return cfg


def test_validate_config_discrete_boundaries() -> None:
    cfg = _valid_runtime_config()
    cfg.serial_port = ""
    with pytest.raises(ValueError, match="serial_port"):
        structures.validate_config(cfg)

    cfg = _valid_runtime_config()
    cfg.cloud_port = 0
    with pytest.raises(ValueError, match="cloud_port"):
        structures.validate_config(cfg)

    cfg = _valid_runtime_config()
    cfg.topic_prefix = ""
    with pytest.raises(ValueError, match="topic_prefix"):
        structures.validate_config(cfg)

    cfg = _valid_runtime_config()
    cfg.status_interval = 0
    with pytest.raises(ValueError, match="status_interval"):
        structures.validate_config(cfg)

    # Disabling the watchdog lifts the constraint on watchdog_interval.
    cfg = _valid_runtime_config()
    cfg.watchdog_enabled = False
    cfg.watchdog_interval = 0.1
    structures.validate_config(cfg)

    cfg = _valid_runtime_config()
    cfg.cloud_spool_dir = "/invalid/flash/path"
    with pytest.raises(ValueError, match="cloud_spool_dir"):
        structures.validate_config(cfg)

    cfg = _valid_runtime_config()
    cfg.file_system_root = "/invalid/root"
    with pytest.raises(ValueError, match="file_system_root"):
        structures.validate_config(cfg)

    cfg = _valid_runtime_config()
    cfg.cloud_certfile = "/path/to/cert"
    cfg.cloud_keyfile = ""
    with pytest.raises(ValueError, match="cloud_certfile"):
        structures.validate_config(cfg)

    cfg = _valid_runtime_config()
    cfg.serial_shared_secret = b""
    with pytest.raises(ValueError, match="serial_shared_secret"):
        structures.validate_config(cfg)


@settings(max_examples=20, derandomize=True, deadline=None)
@given(invalid_port=st.integers(65536, 100000))
def test_validate_config_invalid_cloud_port_property(invalid_port: int) -> None:
    cfg = _valid_runtime_config()
    cfg.cloud_port = invalid_port
    with pytest.raises(ValueError, match="cloud_port"):
        structures.validate_config(cfg)


@settings(max_examples=20, derandomize=True, deadline=None)
@given(invalid_http3_port=st.integers(65536, 100000))
def test_validate_config_invalid_http3_port_property(invalid_http3_port: int) -> None:
    cfg = _valid_runtime_config()
    cfg.cloud_http3_port = invalid_http3_port
    with pytest.raises(ValueError, match="cloud_http3_port"):
        structures.validate_config(cfg)


@settings(max_examples=20, derandomize=True, deadline=None)
@given(interval=st.floats(min_value=0.01, max_value=0.49))
def test_validate_config_invalid_watchdog_interval_property(interval: float) -> None:
    cfg = _valid_runtime_config()
    cfg.watchdog_enabled = True
    cfg.watchdog_interval = interval
    with pytest.raises(ValueError, match="watchdog_interval must be >= 0.5s"):
        structures.validate_config(cfg)


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

    # None and empty variations
    res_none = structures.replace_cloud_publish(original, subscription_identifier=None)
    assert len(res_none.subscription_identifier) == 0
    res_empty = structures.replace_cloud_publish(original, user_properties=[], subscription_identifier=[])
    assert len(res_empty.user_properties) == 0


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

    # context without properties
    class ContextNoProps:
        pass

    assert structures.resolve_cloud_context(msg, ContextNoProps()).topic_name == "initial"


def test_pending_command_methods() -> None:
    cmd = structures.PendingCommand(command_id=1)
    assert cmd.completion.is_set() is False

    cmd.mark_success(b"response")
    assert cmd.success
    assert cmd.response_payload == b"response"
    assert cmd.completion.is_set()

    # Calling mark_success when completion is already set
    cmd.mark_success(b"repeat")
    assert cmd.success

    cmd2 = structures.PendingCommand(command_id=2)
    cmd2.mark_failure(status=404)
    assert not cmd2.success
    assert cmd2.failure_status == 404
    assert cmd2.completion.is_set()
