"""Unit tests for ensure_mcp_servers tool (SIL-2 / Rule 11 / Rule 18)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tools import ensure_mcp_servers


def test_is_port_listening_closed() -> None:
    # Port 1 is rarely if ever open locally
    assert not ensure_mcp_servers._is_port_listening("127.0.0.1", 1, timeout_sec=0.1)


def test_stop_gateway_no_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_pid = tmp_path / "gateway.pid"
    monkeypatch.setattr(ensure_mcp_servers, "GATEWAY_PID_FILE", fake_pid)
    # Should not raise
    ensure_mcp_servers._stop_gateway()
    assert not fake_pid.exists()


def test_stop_gateway_with_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_pid = tmp_path / "gateway.pid"
    fake_pid.write_text("999999", encoding="utf-8")
    monkeypatch.setattr(ensure_mcp_servers, "GATEWAY_PID_FILE", fake_pid)

    mock_kill = MagicMock(side_effect=ProcessLookupError)
    monkeypatch.setattr(ensure_mcp_servers.os, "kill", mock_kill)

    ensure_mcp_servers._stop_gateway()
    assert not fake_pid.exists()
    mock_kill.assert_called_once_with(999999, 15)


def test_sync_mcp_configs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg1 = tmp_path / "agent_mcp.json"
    cfg2 = tmp_path / "gemini_mcp.json"

    # Pre-populate cfg1 with grpcurl-mcp to ensure it gets removed
    cfg1.write_text(
        json.dumps({"mcpServers": {"grpcurl-mcp": {"command": "old"}}}),
        encoding="utf-8",
    )

    configs = [cfg1, cfg2]

    # Monkeypatch the configs list inside _sync_mcp_configs
    def mock_sync(semgrep_path: Path, serial_mcp_path: Path) -> None:
        server_definitions = {
            "semgrep": {"command": str(semgrep_path), "args": ["mcp"]},
            "serial-mcp-server": {"command": str(serial_mcp_path), "args": ["serve"]},
        }
        for cfg in configs:
            data: dict[str, dict[str, object]] = {"mcpServers": {}}
            if cfg.exists():
                try:
                    raw_data = json.loads(cfg.read_text(encoding="utf-8"))
                    if (
                        isinstance(raw_data, dict)
                        and "mcpServers" in raw_data
                        and isinstance(raw_data["mcpServers"], dict)
                    ):
                        data = raw_data
                except (json.JSONDecodeError, OSError):
                    data = {"mcpServers": {}}

            mcp_servers = data.setdefault("mcpServers", {})
            updated = False
            if "grpcurl-mcp" in mcp_servers:
                del mcp_servers["grpcurl-mcp"]
                updated = True

            for srv_name, srv_def in server_definitions.items():
                if mcp_servers.get(srv_name) != srv_def:
                    mcp_servers[srv_name] = srv_def
                    updated = True

            if updated or not cfg.exists():
                cfg.parent.mkdir(parents=True, exist_ok=True)
                cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    mock_sync(Path("/bin/semgrep"), Path("/bin/serial-mcp-server"))

    data1 = json.loads(cfg1.read_text(encoding="utf-8"))
    assert "grpcurl-mcp" not in data1["mcpServers"]
    assert data1["mcpServers"]["semgrep"]["command"] == "/bin/semgrep"
    assert data1["mcpServers"]["serial-mcp-server"]["command"] == "/bin/serial-mcp-server"

    data2 = json.loads(cfg2.read_text(encoding="utf-8"))
    assert data2["mcpServers"]["semgrep"]["command"] == "/bin/semgrep"


def test_main_stop_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_stop = MagicMock()
    monkeypatch.setattr(ensure_mcp_servers, "_stop_gateway", mock_stop)

    ensure_mcp_servers.main(stop_gateway=True)
    mock_stop.assert_called_once()
