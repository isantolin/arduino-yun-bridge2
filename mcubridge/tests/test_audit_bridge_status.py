import json
from pathlib import Path
from typer.testing import CliRunner
from tools.audit_bridge_status import app, audit_status_dict

from typing import Any, cast

runner = CliRunner()


def test_audit_status_dict_clean() -> None:
    data = {
        "metrics": {
            "cloud_spool_dropped_limit": 0,
            "cloud_spool_trim_events": 0,
            "queue_depths": {"mailbox_outgoing": 0},
        },
        "bridge": {
            "serial_link": {"connected": True},
            "handshake": {
                "failures": 0,
                "failure_streak": 0,
                "last_error": "",
                "fatal_count": 0,
            },
        },
    }
    errors = audit_status_dict(data)
    assert errors == []


def test_audit_status_dict_anomalies() -> None:
    # 1. Invalid root types
    assert "not a dictionary" in audit_status_dict({"metrics": "invalid"})[0]
    assert "not a dictionary" in audit_status_dict({"metrics": {}, "bridge": "invalid"})[0]

    # 2. Spool anomalies
    spool_data = {
        "metrics": {
            "cloud_spool_dropped_limit": 5,
            "cloud_spool_trim_events": 2,
        },
        "bridge": {},
    }
    spool_errors = audit_status_dict(spool_data)
    assert len(spool_errors) == 2
    assert any("dropped messages" in e for e in spool_errors)
    assert any("trim events" in e for e in spool_errors)

    # 3. Serial link and handshake anomalies
    link_data = {
        "metrics": {},
        "bridge": {
            "serial_link": {"connected": False},
            "handshake": {
                "failures": 3,
                "failure_streak": 3,
                "last_error": "link_sync_timeout",
                "fatal_count": 1,
                "fatal_reason": "link_sync_timeout",
                "fatal_detail": "streak_exceeded",
            },
        },
    }
    link_errors = audit_status_dict(link_data)
    assert len(link_errors) == 5
    assert any("disconnected" in e for e in link_errors)
    assert any("link_sync_timeout" in e for e in link_errors)
    assert any("failure streak active" in e for e in link_errors)
    assert any("Fatal handshake" in e for e in link_errors)


def test_audit_cli_raw_json_clean() -> None:
    raw = json.dumps({
        "metrics": {"cloud_spool_dropped_limit": 0},
        "bridge": {"serial_link": {"connected": True}, "handshake": {"failures": 0}},
    })
    result = runner.invoke(cast(Any, app), ["--raw-json", raw])
    assert result.exit_code == 0
    assert "STATUS AUDIT PASS" in result.stdout


def test_audit_cli_raw_json_with_errors() -> None:
    raw = json.dumps({
        "metrics": {"cloud_spool_dropped_limit": 12},
        "bridge": {"handshake": {"last_error": "link_sync_timeout"}},
    })
    result = runner.invoke(cast(Any, app), ["--raw-json", raw])
    assert result.exit_code == 1
    assert "STATUS AUDIT FAIL" in result.stderr
    assert "dropped messages" in result.stderr


def test_audit_cli_file(tmp_path: Path) -> None:
    clean_file = tmp_path / "status.json"
    clean_file.write_text(
        json.dumps({
            "metrics": {},
            "bridge": {"serial_link": {"connected": True}},
        }),
        encoding="utf-8",
    )
    result = runner.invoke(cast(Any, app), ["--status-path", str(clean_file)])
    assert result.exit_code == 0

    # Nonexistent file
    missing = runner.invoke(cast(Any, app), ["--status-path", str(tmp_path / "nonexistent.json")])
    assert missing.exit_code == 1
    assert "not found" in missing.stderr

    # Corrupt json file
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("invalid json {{{", encoding="utf-8")
    corrupt_res = runner.invoke(cast(Any, app), ["--status-path", str(corrupt)])
    assert corrupt_res.exit_code == 1
    assert "Failed to parse" in corrupt_res.stderr
