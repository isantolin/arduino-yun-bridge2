"""Property-based tests for CLOUD topics module using Hypothesis. [SIL-2]"""

from __future__ import annotations

from typing import Any, cast

from hypothesis import given
from hypothesis import strategies as st
from mcubridge.protocol import mcu_wire_pb2 as pb
from mcubridge.protocol.protocol import (
    OPERATIONS,
    get_operation,
    get_topic_for_message,
    topic_path,
)

_SAFE_TEXT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-",
    min_size=1,
    max_size=16,
)

_KNOWN_OP_NAMES = [spec.name for spec in OPERATIONS if spec.cloud_topic is not None]


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
        topic_path("prefix", "evt", "rpc.pb.SystemService", "HandshakeSnapshot")
        == "prefix/evt/rpc.pb.SystemService/HandshakeSnapshot"
    )
    assert topic_path("p", "req", "rpc.pb.GpioService", "DigitalRead") == "p/req/rpc.pb.GpioService/DigitalRead"
    assert topic_path("p", "res", "rpc.pb.SpiService", "Transfer") == "p/res/rpc.pb.SpiService/Transfer"
    assert topic_path("p", "req", "rpc.pb.DataStoreService", "Get") == "p/req/rpc.pb.DataStoreService/Get"
    assert topic_path("p", "req", "rpc.pb.ProcessService", "Kill") == "p/req/rpc.pb.ProcessService/Kill"
    assert topic_path("p", "evt", "rpc.pb.MailboxService", "Push") == "p/evt/rpc.pb.MailboxService/Push"


def test_topics_edge_cases() -> None:
    """Validate topic path formatting edge cases."""
    assert topic_path("", "SystemService") == "SystemService"
    assert topic_path("", "SystemService", "") == "SystemService"
    assert topic_path("", "") == ""


def test_get_topic_for_message_all_types() -> None:
    """[SIL-2] Test get_topic_for_message across all message input types (str, message, class, invalid)."""
    assert get_topic_for_message("bridge", "VersionResponse") == "bridge/evt/rpc.pb.SystemService/VersionResponse"
    assert get_topic_for_message("bridge", "NonExistentMsg") is None

    resp = pb.VersionResponse()
    assert get_topic_for_message("bridge", resp) == "bridge/evt/rpc.pb.SystemService/VersionResponse"

    assert get_topic_for_message("bridge", pb.VersionResponse) == "bridge/evt/rpc.pb.SystemService/VersionResponse"

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
