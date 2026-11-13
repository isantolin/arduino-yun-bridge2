"""Property-based tests for CLOUD topics module using Hypothesis. [SIL-2]"""

from __future__ import annotations

from typing import Any, cast

from hypothesis import assume, given
from hypothesis import strategies as st
from mcubridge.protocol import mcu_wire_pb2 as pb
from mcubridge.protocol.protocol import (
    OPERATIONS,
    get_operation,
    get_topic_for_message,
    parse_topic,
    topic_path,
)

_SAFE_TEXT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-",
    min_size=1,
    max_size=16,
)


@given(
    prefix=_SAFE_TEXT,
    direction=st.sampled_from(["req", "res", "evt"]),
    method=_SAFE_TEXT,
)
def test_topic_path_parse_topic_isomorphism(prefix: str, direction: str, method: str) -> None:
    """Property: Any valid (prefix, direction, method) tuple roundtrips through parse_topic idempotently."""
    raw = topic_path(prefix, direction, method)
    route = parse_topic(prefix, raw)
    assert route is not None
    assert route.raw == raw
    assert route.prefix == prefix
    assert route.direction == direction
    assert route.method == method


@given(
    prefix_a=_SAFE_TEXT,
    prefix_b=_SAFE_TEXT,
    direction=st.sampled_from(["req", "res", "evt"]),
    method=_SAFE_TEXT,
)
def test_parse_topic_mismatched_prefix_rejected(prefix_a: str, prefix_b: str, direction: str, method: str) -> None:
    """Property: A topic constructed with prefix_b is deterministically rejected when parsed with distinct prefix_a."""
    assume(prefix_a != prefix_b)
    raw = topic_path(prefix_b, direction, method)
    assert parse_topic(prefix_a, raw) is None


_KNOWN_OP_NAMES = [spec.name for spec in OPERATIONS if spec.cloud_topic is not None]


@given(
    prefix=_SAFE_TEXT,
    invalid_direction=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=4, max_size=16).filter(
        lambda s: s not in ("req", "res", "evt")
    ),
    method=_SAFE_TEXT,
)
def test_parse_topic_unrecognized_direction_rejected(prefix: str, invalid_direction: str, method: str) -> None:
    """Property: Any direction segment not in ('req', 'res', 'evt') returns None."""
    raw = topic_path(prefix, invalid_direction, method)
    assert parse_topic(prefix, raw) is None


@given(s=_SAFE_TEXT)
def test_parse_topic_empty_inputs_rejected(s: str) -> None:
    """Property: Empty prefix or empty topic string always returns None."""
    assert parse_topic("", s) is None
    assert parse_topic(s, "") is None
    assert parse_topic("", "") is None
    assert parse_topic("prefix", "///") is None
    assert parse_topic("///", "prefix/req/rpc.pb.GpioService/DigitalWrite") is None


@given(
    prefix=_SAFE_TEXT,
    op_name=st.sampled_from(_KNOWN_OP_NAMES),
)
def test_get_topic_for_message_op_name_known(prefix: str, op_name: str) -> None:
    """Property: get_topic_for_message returns exact topic path for all registered operations."""
    res = get_topic_for_message(prefix, op_name)
    assert res is not None
    assert res.startswith(prefix)
    spec = get_operation(op_name)
    assert spec is not None
    expected_topic = spec.cloud_topic
    assert expected_topic is not None
    assert expected_topic in res


@given(
    prefix=_SAFE_TEXT,
    unknown_id=st.integers(min_value=60000, max_value=65535),
)
def test_get_topic_for_message_unknown_id(prefix: str, unknown_id: int) -> None:
    """Property: get_topic_for_message returns None for any unregistered operation."""
    assert get_topic_for_message(prefix, unknown_id) is None


def test_topics_canonical_service_paths() -> None:
    """Validate canonical service topic paths."""
    assert (
        topic_path("prefix", "evt", "SystemService", "HandshakeSnapshot")
        == "prefix/evt/SystemService/HandshakeSnapshot"
    )
    assert topic_path("p", "req", "rpc.pb.GpioService", "DigitalRead") == "p/req/rpc.pb.GpioService/DigitalRead"
    assert topic_path("p", "res", "SpiService", "Transfer") == "p/res/SpiService/Transfer"
    assert topic_path("p", "req", "rpc.pb.DataStoreService", "Get") == "p/req/rpc.pb.DataStoreService/Get"
    assert topic_path("p", "req", "rpc.pb.ProcessService", "Kill") == "p/req/rpc.pb.ProcessService/Kill"
    assert topic_path("p", "evt", "MailboxService", "Push") == "p/evt/MailboxService/Push"


def test_topics_edge_cases() -> None:
    """Validate topic parsing and formatting edge cases."""
    assert parse_topic("br", "br/invalid_topic/foo") is None
    assert parse_topic("br", "br/only_one_segment") is None
    assert parse_topic("br", "devices/arduino-01/invalid_dir/rpc.pb.GpioService/DigitalWrite") is None
    assert parse_topic("", "foo") is None
    assert parse_topic("br", "") is None
    assert topic_path("", "SystemService") == "SystemService"
    assert topic_path("", "SystemService", "") == "SystemService"


def test_get_topic_for_message_all_types() -> None:
    """[SIL-2] Test get_topic_for_message across all message input types (str, message, class, invalid)."""
    assert get_topic_for_message("bridge", "VersionResponse") == "bridge/evt/SystemService/VersionResponse"
    assert get_topic_for_message("bridge", "NonExistentMsg") is None

    resp = pb.VersionResponse()
    assert get_topic_for_message("bridge", resp) == "bridge/evt/SystemService/VersionResponse"

    assert get_topic_for_message("bridge", pb.VersionResponse) == "bridge/evt/SystemService/VersionResponse"

    class DummyMsg:
        class DESCRIPTOR:
            name = "NonExistent"

    assert get_topic_for_message("bridge", cast(Any, DummyMsg)) is None
    assert get_topic_for_message("bridge", cast(Any, 3.14)) is None


def test_registry_pigweed_descriptors_proto_ssot() -> None:
    """[SIL-2] Verify that canonical Pigweed descriptors resolve correctly without aliases."""
    spec_dw = get_operation("rpc.pb.GpioService/DigitalWrite")
    assert spec_dw is not None
    assert spec_dw.method_name == "DigitalWrite"
    assert get_operation("GpioService/DigitalWrite") is None
    assert get_operation("DigitalWrite") is None
    assert get_operation(pb.DigitalWrite) is spec_dw
    if spec_dw.service_id and spec_dw.method_id:
        assert get_operation((spec_dw.service_id, spec_dw.method_id)) is spec_dw

    spec_ds = get_operation("rpc.pb.DataStoreService/Put")
    assert spec_ds is not None
    assert spec_ds.method_name == "Put"
    assert get_operation("DataStoreService/Put") is None
    assert get_operation("DatastorePut") is None
    assert get_operation(pb.DatastorePut) is spec_ds
    assert get_operation("Put") is None
    if spec_ds.service_id and spec_ds.method_id:
        assert get_operation((spec_ds.service_id, spec_ds.method_id)) is spec_ds


def test_registry_fleet_and_direction_topic_parsing() -> None:
    """[SIL-2] Verify parsing of fleet and direction-based canonical topics."""
    # 1. Fleet topic: devices/{device_id}/{direction}/{method}
    route_fleet = parse_topic("any_prefix", "devices/arduino-01/req/rpc.pb.GpioService/DigitalWrite")
    assert route_fleet is not None
    assert route_fleet.device_id == "arduino-01"
    assert route_fleet.direction == "req"
    assert route_fleet.method == "rpc.pb.GpioService/DigitalWrite"
    assert route_fleet.prefix == "devices/arduino-01"
    assert route_fleet.action == "DigitalWrite"

    # 2. Canonical Direction topic: {prefix}/{direction}/{method}
    route_dir = parse_topic("yun/bridge", "yun/bridge/req/rpc.pb.GpioService/DigitalWrite")
    assert route_dir is not None
    assert route_dir.device_id == ""
    assert route_dir.direction == "req"
    assert route_dir.method == "rpc.pb.GpioService/DigitalWrite"
    assert route_dir.prefix == "yun/bridge"
    assert route_dir.action == "DigitalWrite"
