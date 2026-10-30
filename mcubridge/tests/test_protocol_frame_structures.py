"""Exhaustive tests for mcubridge.protocol.structures. [SIL-2]"""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import REGISTRY, TopicRoute


def test_topic_route_properties() -> None:
    route = TopicRoute(
        raw="devices/dev1/req/gpio.write",
        prefix="devices/dev1",
        device_id="dev1",
        direction="req",
        method="gpio.write",
    )
    assert route.device_id == "dev1"
    assert route.direction == "req"
    assert route.method == "gpio.write"
    assert route.action == "write"

    route_empty = TopicRoute()
    assert route_empty.method == ""
    assert route_empty.action is None


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


def test_allows_method() -> None:
    auth = pb.TopicAuthorization(digital_read=True, digital_write=False)
    spec_read = REGISTRY.get(pb.Command.CMD_DIGITAL_READ)
    spec_write = REGISTRY.get(pb.Command.CMD_DIGITAL_WRITE)
    assert spec_read is not None
    assert REGISTRY.allows_method(auth, spec_read) is True
    assert spec_write is not None
    assert REGISTRY.allows_method(auth, spec_write) is False


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
    cfg.process_timeout = 10
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


@given(invalid_port=st.integers(65536, 100000))
def test_validate_config_invalid_cloud_port_property(invalid_port: int) -> None:
    cfg = _valid_runtime_config()
    cfg.cloud_port = invalid_port
    with pytest.raises(ValueError, match="cloud_port"):
        structures.validate_config(cfg)


@given(invalid_http3_port=st.integers(65536, 100000))
def test_validate_config_invalid_http3_port_property(invalid_http3_port: int) -> None:
    cfg = _valid_runtime_config()
    cfg.cloud_http3_port = invalid_http3_port
    with pytest.raises(ValueError, match="cloud_http3_port"):
        structures.validate_config(cfg)


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


def test_get_ssl_context_insecure() -> None:

    cfg_insecure = pb.RuntimeConfig(cloud_tls=True, cloud_tls_insecure=True)
    ctx = structures.get_ssl_context(cfg_insecure)
    assert ctx is not None
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_protocol_frame_validation_error_paths() -> None:
    import struct
    from binascii import crc32
    from mcubridge.protocol import frame, protocol

    with pytest.raises(ValueError, match="Invalid command ID"):
        frame.build_frame(-1, 1)

    with pytest.raises(ValueError, match="Invalid command ID"):
        frame.build_frame(protocol.UINT16_MAX + 1, 1)

    with pytest.raises(ValueError, match="Invalid sequence ID"):
        frame.build_frame(1, -1)

    with pytest.raises(ValueError, match="Invalid sequence ID"):
        frame.build_frame(1, protocol.UINT16_MAX + 1)

    env = pb.RpcEnvelope(version=99, command_id=1, sequence_id=1)
    body = env.SerializeToString()
    bad_ver_frame = body + struct.pack("<I", crc32(body) & protocol.CRC32_MASK)
    with pytest.raises(ValueError, match="Unsupported protocol version"):
        frame.parse_frame(bad_ver_frame)

    # Protobuf parsing and CRC verification accept a buffer view without a bytes copy.
    session_key = b"k" * protocol.AEAD_KEY_SIZE
    nonce = b"n" * protocol.AEAD_NONCE_SIZE
    encoded = frame.build_frame(0x1234, 0x1234, b"payload", nonce=nonce, session_key=session_key)
    decoded = frame.parse_frame(memoryview(encoded), session_key)
    assert decoded.envelope.command_id == 0x1234
    assert decoded.envelope.sequence_id == 0x1234
    assert decoded.payload == b"payload"


def test_protocol_frame_and_structures_edge_branches(tmp_path: Path) -> None:
    from mcubridge.protocol import frame, protocol

    # 1. frame.build_frame with encrypted payload exceeding MAX_PAYLOAD_SIZE (line 87)
    with pytest.raises(ValueError, match="exceeds maximum"):
        frame.build_frame(
            command_id=1,
            sequence_id=1,
            payload=b"X" * (protocol.MAX_PAYLOAD_SIZE + 1),
            session_key=b"k" * 32,
        )

    # 2. frame.build_frame with unencrypted protobuf message not in PAYLOAD_FIELD_MAP (line 98->105)
    unmapped_msg = pb.RuntimeConfig(serial_port="/dev/dummy")
    raw_frame = frame.build_frame(command_id=1, sequence_id=1, payload=unmapped_msg)
    assert len(raw_frame) > 0

    # 3. structures.get_ssl_context with valid cafile
    ca_file = tmp_path / "test_ca.crt"
    ca_file.write_text("dummy ca content")
    with pytest.raises(RuntimeError, match="TLS setup failed"):
        structures.get_ssl_context(pb.RuntimeConfig(cloud_tls=True, cloud_cafile=str(ca_file)))

    # 6. structures.validate_config with cloud_http3_port=0 and >0 (line 159->162)
    cfg_http3_zero = pb.RuntimeConfig(
        serial_port="/dev/ttyATH0",
        cloud_port=8443,
        process_timeout=10,
        topic_prefix="mcu",
        status_interval=1,
        serial_shared_secret=b"dummy_secret_123",
        cloud_http3_port=0,
    )
    structures.validate_config(cfg_http3_zero)
    assert cfg_http3_zero.cloud_http3_port == 0

    cfg_http3_active = pb.RuntimeConfig(
        serial_port="/dev/ttyATH0",
        cloud_port=8443,
        process_timeout=10,
        topic_prefix="mcu",
        status_interval=1,
        serial_shared_secret=b"dummy_secret_123",
        cloud_http3_port=8443,
    )
    structures.validate_config(cfg_http3_active)
    assert cfg_http3_active.cloud_http3_port == 8443


def test_structures_coverage_boost() -> None:
    qp = structures.create_queued_publish(topic_name="topic", payload=b"payload")
    assert qp.topic_name == "topic"
    assert qp.payload == b"payload"
    assert qp.qos == 1

    ta = pb.TopicAuthorization()
    ta.file_read = True
    assert ta.file_read

    class MockAllowAllAuth:
        allow_all = True

    spec_write = REGISTRY.get(pb.Command.CMD_DIGITAL_WRITE)
    assert spec_write is not None
    assert REGISTRY.allows_method(MockAllowAllAuth(), spec_write) is True
    assert REGISTRY.get(pb.Command.CMD_DIGITAL_WRITE) is not None
    assert REGISTRY.get(pb.DigitalWrite) is not None
    assert REGISTRY.get(pb.DigitalWrite()) is not None
