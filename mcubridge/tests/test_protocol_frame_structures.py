"""Exhaustive tests for mcubridge.protocol.structures. [SIL-2]"""

from __future__ import annotations

import ssl
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import structures


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


def test_tls_session_ticket_uncovered_branches() -> None:
    import ssl
    from unittest.mock import MagicMock

    cfg_insecure = pb.RuntimeConfig(cloud_tls=True, cloud_tls_insecure=True)
    ctx = structures.get_ssl_context(cfg_insecure)
    assert ctx is not None
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE

    structures.save_tls_session_ticket(None, "host", 443, b"ticket")
    structures.save_tls_session_ticket(object(), "host", 443, b"")
    assert structures.load_tls_session_ticket(None, "host", 443) is None

    mem_cache = MagicMock()
    mem_cache.is_mem = True
    mem_cache._mem = {}
    structures.save_tls_session_ticket(mem_cache, "host", 443, b"ticket")
    assert structures.load_tls_session_ticket(mem_cache, "host", 443) == b"ticket"

    disk_cache = MagicMock()
    disk_cache.is_mem = False
    disk_cache.env = MagicMock()
    disk_cache.db = MagicMock()
    disk_cache.env.begin.side_effect = OSError("write error")
    structures.save_tls_session_ticket(disk_cache, "host", 443, b"ticket")
    assert structures.load_tls_session_ticket(disk_cache, "host", 443) is None

    valid_disk_cache = MagicMock()
    valid_disk_cache.is_mem = False
    valid_disk_cache.env = MagicMock()
    valid_disk_cache.db = MagicMock()
    mock_txn = MagicMock()
    mock_txn.get.return_value = b"ticket-from-disk"
    valid_disk_cache.env.begin.return_value.__enter__.return_value = mock_txn
    structures.save_tls_session_ticket(valid_disk_cache, "host", 443, b"ticket-from-disk")
    mock_txn.put.assert_called_once_with(b"tls_ticket:host:443", b"ticket-from-disk")
    assert structures.load_tls_session_ticket(valid_disk_cache, "host", 443) == b"ticket-from-disk"

    mock_txn.get.return_value = None
    assert structures.load_tls_session_ticket(valid_disk_cache, "host", 443) is None


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
    unmapped_msg = pb.DatastorePut(key="mykey", value=b"myval")
    raw_frame = frame.build_frame(command_id=1, sequence_id=1, payload=unmapped_msg)
    assert len(raw_frame) > 0

    # 3. structures._build_cached_ssl_context with valid cafile (line 179)
    ca_file = tmp_path / "test_ca.crt"
    ca_file.write_text("dummy ca content")
    build_ctx = getattr(structures, "_build_cached_ssl_context")
    with pytest.raises(ssl.SSLError):
        # ssl.create_default_context with dummy ca will raise SSLError but covers line 179
        build_ctx(str(ca_file), "", "", False)

    # 4. structures.save_tls_session_ticket on cache without _mem (line 213->215)
    mock_env_cache = MagicMock()
    delattr(mock_env_cache, "_mem")
    structures.save_tls_session_ticket(mock_env_cache, "host1", 443, b"ticket")
    assert mock_env_cache.env.begin.called

    # 5. structures.load_tls_session_ticket on cache without _mem or env (line 240)
    assert structures.load_tls_session_ticket(object(), "host2", 443) is None
