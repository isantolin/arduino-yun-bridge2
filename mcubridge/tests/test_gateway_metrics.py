"""Unit tests for Pigweed MetricService gateway auditing in tools.emulation.pw_emu_runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tools.emulation import pw_emu_runner

_ANOMALY_CASES: list[tuple[dict[str, Any], int, int]] = [
    ({}, 1, 0),
    ({"gateway": "not-a-dict"}, 1, 0),
    (
        {
            "gateway": {
                "device_sessions_opened": 0,
                "client_sessions_opened": 1,
                "dispatches_total": 1,
                "client_rejections_total": 0,
                "acl_denials_total": 0,
            }
        },
        1,
        0,
    ),
    (
        {
            "gateway": {
                "device_sessions_opened": 1,
                "client_sessions_opened": 0,
                "dispatches_total": 1,
                "client_rejections_total": 0,
                "acl_denials_total": 0,
            }
        },
        1,
        0,
    ),
    (
        {
            "gateway": {
                "device_sessions_opened": 1,
                "client_sessions_opened": 1,
                "dispatches_total": 0,
                "client_rejections_total": 0,
                "acl_denials_total": 0,
            }
        },
        1,
        1,
    ),
    (
        {
            "gateway": {
                "device_sessions_opened": 1,
                "client_sessions_opened": 1,
                "dispatches_total": 2,
                "client_rejections_total": 1,
                "acl_denials_total": 0,
            }
        },
        1,
        1,
    ),
    (
        {
            "gateway": {
                "device_sessions_opened": 1,
                "client_sessions_opened": 1,
                "dispatches_total": 2,
                "client_rejections_total": 0,
                "acl_denials_total": 2,
            }
        },
        1,
        1,
    ),
]


def test_audit_gateway_pw_metrics_passes_on_healthy_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Healthy gateway metrics satisfy session, dispatch, and zero-denial thresholds."""
    snapshot: dict[str, Any] = {
        "gateway": {
            "device_sessions_opened": 3,
            "client_sessions_opened": 2,
            "dispatches_total": 5,
            "client_rejections_total": 0,
            "acl_denials_total": 0,
        }
    }

    def _fake_fetch(**_kwargs: Any) -> dict[str, Any]:
        return snapshot

    monkeypatch.setattr(pw_emu_runner, "fetch_gateway_pw_metrics", _fake_fetch)

    assert pw_emu_runner.audit_gateway_pw_metrics(min_device_sessions=3, min_dispatches=3) is True


@pytest.mark.parametrize(("snapshot", "min_devices", "min_dispatches"), _ANOMALY_CASES)
def test_audit_gateway_pw_metrics_rejects_anomalies(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: dict[str, Any],
    min_devices: int,
    min_dispatches: int,
) -> None:
    """Missing gateway group, insufficient counters, or security rejections fail the audit."""

    def _fake_fetch(**_kwargs: Any) -> dict[str, Any]:
        return snapshot

    monkeypatch.setattr(pw_emu_runner, "fetch_gateway_pw_metrics", _fake_fetch)

    assert (
        pw_emu_runner.audit_gateway_pw_metrics(
            min_device_sessions=min_devices,
            min_dispatches=min_dispatches,
        )
        is False
    )


def test_audit_gateway_pw_metrics_handles_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport or RPC exceptions return False cleanly without crashing teardown."""

    def _boom(**_kwargs: Any) -> dict[str, Any]:
        raise OSError("connection refused")

    monkeypatch.setattr(pw_emu_runner, "fetch_gateway_pw_metrics", _boom)

    assert pw_emu_runner.audit_gateway_pw_metrics() is False


def test_fetch_gateway_pw_metrics_uses_detokenizer_and_closes_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """fetch_gateway_pw_metrics parses the MetricService tree and always closes its TLS session."""
    closed: list[bool] = []
    db_args: list[Path | None] = []

    class _FakeChannel:
        rpcs: object = object()

    class _FakeRpcClient:
        def channel(self, _cid: int) -> _FakeChannel:
            return _FakeChannel()

    class _FakeSession:
        def __init__(self, _sock: Any) -> None:
            self.errors: list[OSError] = []
            self.rpc_client = _FakeRpcClient()

        def close(self) -> None:
            closed.append(True)

    def _fake_build_detokenizer(*, token_database: Path | None = None) -> tuple[object, Path | None]:
        db_args.append(token_database)
        return object(), token_database

    def _fake_open_socket(_host: str, _port: int, _timeout_s: float) -> Any:
        return object()

    def _fake_collect_metrics(_rpcs: Any, _detok: Any, *, timeout_s: float = 5.0) -> dict[str, Any]:
        assert timeout_s > 0
        return {"gateway": {"dispatches_total": 4}}

    monkeypatch.setattr(pw_emu_runner, "open_tls_client_socket", _fake_open_socket)
    monkeypatch.setattr(pw_emu_runner, "TlsMetricSession", _FakeSession)
    monkeypatch.setattr(pw_emu_runner, "build_detokenizer", _fake_build_detokenizer)
    monkeypatch.setattr(pw_emu_runner, "collect_metrics_tree", _fake_collect_metrics)

    missing_bin = tmp_path / "missing-gateway"
    res = pw_emu_runner.fetch_gateway_pw_metrics(gateway_binary=missing_bin)
    assert res == {"gateway": {"dispatches_total": 4}}
    assert closed == [True]
    assert db_args == [None]

    fake_bin = tmp_path / "mcubridge-gateway-cpp"
    fake_bin.write_bytes(b"ELF")
    res2 = pw_emu_runner.fetch_gateway_pw_metrics(gateway_binary=fake_bin)
    assert res2 == {"gateway": {"dispatches_total": 4}}
    assert db_args == [None, fake_bin]
