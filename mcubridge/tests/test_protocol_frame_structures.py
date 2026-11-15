"""Exhaustive tests for mcubridge.protocol.structures. [SIL-2]"""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mcubridge.protocol import cloud_gateway_pb2, config_schema_pb2, mcu_wire_pb2
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import TopicRoute, allows_method, get_operation


def test_topic_route_properties() -> None:
    route = TopicRoute(
        raw="devices/dev1/req/rpc.pb.GpioService/DigitalWrite",
        prefix="devices/dev1",
        device_id="dev1",
        direction="req",
        method="rpc.pb.GpioService/DigitalWrite",
    )
    assert route.device_id == "dev1"
    assert route.direction == "req"
    assert route.method == "rpc.pb.GpioService/DigitalWrite"
    assert route.action == "DigitalWrite"

    route_empty = TopicRoute()
    assert route_empty.method == ""
    assert route_empty.action is None


@given(
    cmd=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=16),
    arg=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=0, max_size=16),
)
def test_is_command_allowed_property(cmd: str, arg: str) -> None:
    policy = config_schema_pb2.AllowedCommandPolicy(entries=[cmd, "*"])
    full_cmd = f"{cmd} {arg}".strip()
    assert structures.is_command_allowed(policy, full_cmd) is True
    assert structures.is_command_allowed(policy, "") is False

    strict_policy = config_schema_pb2.AllowedCommandPolicy(entries=[cmd])
    assert structures.is_command_allowed(strict_policy, full_cmd) is True
    assert structures.is_command_allowed(strict_policy, f"disallowed_{cmd}") is False


def test_create_allowed_policy() -> None:
    policy_wildcard = structures.create_allowed_policy(["ls", "*", "cat"])
    assert policy_wildcard.entries == ["*"]

    policy_sorted = structures.create_allowed_policy(["cat, ls", "grep"])
    assert policy_sorted.entries == ["cat", "grep", "ls"]


def test_allows_method() -> None:
    auth = config_schema_pb2.TopicAuthorization(digital_read=True, digital_write=False)
    spec_read = get_operation("rpc.pb.GpioService/DigitalRead")
    spec_write = get_operation("rpc.pb.GpioService/DigitalWrite")
    assert spec_read is not None
    assert allows_method(auth, spec_read) is True
    assert spec_write is not None
    assert allows_method(auth, spec_write) is False


def _valid_runtime_config() -> config_schema_pb2.RuntimeConfig:
    """Build a RuntimeConfig satisfying all validation rules."""
    cfg = config_schema_pb2.RuntimeConfig()
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

    # Empty secret explicitly selects the protocol's unauthenticated mode.
    cfg = _valid_runtime_config()
    cfg.serial_shared_secret = b""
    structures.validate_config(cfg)
    assert cfg.serial_shared_secret == b""


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
    cfg = config_schema_pb2.RuntimeConfig(cloud_tls=False)
    assert structures.get_ssl_context(cfg) is None

    cfg_tls = config_schema_pb2.RuntimeConfig(
        cloud_tls=True,
        cloud_cafile="/nonexistent/ca.pem",
    )
    with pytest.raises(RuntimeError, match="Cloud TLS CA file missing|TLS setup failed"):
        structures.get_ssl_context(cfg_tls)

    cfg_mtls_invalid = config_schema_pb2.RuntimeConfig(
        cloud_tls=True,
        cloud_certfile="/path/to/cert",
        cloud_keyfile="",
    )
    with pytest.raises(RuntimeError, match="Both cloud_certfile and cloud_keyfile|TLS setup failed"):
        structures.get_ssl_context(cfg_mtls_invalid)


def test_resolve_cloud_context() -> None:
    msg = cloud_gateway_pb2.CloudQueuedPublish(topic_name="initial", payload=b"p")

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


def test_get_ssl_context_insecure() -> None:

    cfg_insecure = config_schema_pb2.RuntimeConfig(cloud_tls=True, cloud_tls_insecure=True)
    ctx = structures.get_ssl_context(cfg_insecure)
    assert ctx is not None
    assert ctx.check_hostname is False
    # Only hostname matching is relaxed; the certificate chain must still be verified.
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_protocol_hdlc_encoding_decoding_roundtrip() -> None:
    from pw_hdlc.decode import FrameDecoder
    from pw_rpc.internal.packet_pb2 import RpcPacket
    from mcubridge.protocol import protocol

    # 1. RPC HDLC encoding & decoding
    pin_msg = mcu_wire_pb2.PinRead(pin=13)
    encoded_rpc = protocol.encode_hdlc_rpc("rpc.pb.GpioService/DigitalRead", pin_msg, call_id=42)
    assert encoded_rpc.startswith(b"\x7e")
    assert encoded_rpc.endswith(b"\x7e")

    decoder = FrameDecoder()
    frames = list(decoder.process(encoded_rpc))
    assert len(frames) == 1
    frame = frames[0]
    assert frame.ok()
    assert frame.address == protocol.HDLC_ADDRESS_RPC

    pkt = RpcPacket()
    pkt.ParseFromString(frame.data)
    assert pkt.call_id == 42
    decoded_pin = mcu_wire_pb2.PinRead()
    decoded_pin.ParseFromString(pkt.payload)
    assert decoded_pin.pin == 13

    # 2. Console HDLC encoding & decoding
    encoded_console = protocol.encode_hdlc_console(b"hello world\n")
    decoder_console = FrameDecoder()
    console_frames = list(decoder_console.process(encoded_console))
    assert len(console_frames) == 1
    assert console_frames[0].ok()
    assert console_frames[0].address == protocol.HDLC_ADDRESS_CONSOLE
    assert console_frames[0].data == b"hello world\n"

    # 3. Corrupted HDLC frame FCS error
    corrupted = bytearray(encoded_rpc)
    corrupted[-3] ^= 0xFF
    decoder_bad = FrameDecoder()
    bad_frames = list(decoder_bad.process(bytes(corrupted)))
    assert len(bad_frames) == 1
    assert not bad_frames[0].ok()


def test_protocol_frame_and_structures_edge_branches(tmp_path: Path) -> None:

    # 3. structures.get_ssl_context with valid cafile
    ca_file = tmp_path / "test_ca.crt"
    ca_file.write_text("dummy ca content")
    with pytest.raises(RuntimeError, match="TLS setup failed"):
        structures.get_ssl_context(config_schema_pb2.RuntimeConfig(cloud_tls=True, cloud_cafile=str(ca_file)))

    # 6. structures.validate_config with cloud_http3_port=0 and >0 (line 159->162)
    cfg_http3_zero = config_schema_pb2.RuntimeConfig(
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

    cfg_http3_active = config_schema_pb2.RuntimeConfig(
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

    ta = config_schema_pb2.TopicAuthorization()
    ta.file_read = True
    assert ta.file_read

    class MockAllowAllAuth:
        allow_all = True

    spec_write = get_operation("rpc.pb.GpioService/DigitalWrite")
    assert spec_write is not None
    assert allows_method(MockAllowAllAuth(), spec_write) is True
    assert get_operation("rpc.pb.GpioService/DigitalWrite") is not None
    assert get_operation(mcu_wire_pb2.DigitalWrite) is not None
    assert get_operation(mcu_wire_pb2.DigitalWrite()) is not None
