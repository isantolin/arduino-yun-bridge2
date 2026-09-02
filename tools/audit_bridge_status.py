#!/usr/bin/env python3
"""Audit McuBridge status snapshot for SIL-2 / MIL-SPEC integrity and error-free operation."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any, cast

import typer

app = typer.Typer(
    help="Audit /tmp/mcubridge_status.json for errors, handshake failures, and metric anomalies.",
    add_completion=False,
)


def audit_status_dict(data: dict[str, Any]) -> list[str]:
    """Inspect status dictionary and return list of error descriptions."""
    errors: list[str] = []

    metrics_raw = data.get("metrics", {})
    if not isinstance(metrics_raw, dict):
        return ["Status 'metrics' field is not a dictionary"]
    metrics = cast(dict[str, object], metrics_raw)

    # 1. Cloud spool drop / trim anomalies
    dropped_raw = metrics.get("cloud_spool_dropped_limit", 0)
    dropped: int = int(cast(int, dropped_raw)) if isinstance(dropped_raw, (int, float)) else 0
    if dropped > 0:
        errors.append(f"Cloud spool dropped messages limit exceeded (count={dropped})")

    trimmed_raw = metrics.get("cloud_spool_trim_events", 0)
    trimmed: int = int(cast(int, trimmed_raw)) if isinstance(trimmed_raw, (int, float)) else 0
    if trimmed > 0:
        errors.append(f"Cloud spool trim events occurred (count={trimmed})")

    bridge_raw = data.get("bridge", {})
    if not isinstance(bridge_raw, dict):
        return ["Status 'bridge' field is not a dictionary"]
    bridge = cast(dict[str, object], bridge_raw)

    # 2. Serial link state
    serial_link_raw = bridge.get("serial_link", {})
    if isinstance(serial_link_raw, dict) and serial_link_raw:
        serial_link = cast(dict[str, object], serial_link_raw)
        if not bool(serial_link.get("connected", False)):
            errors.append("Serial link is reported as disconnected")

    # 3. Handshake failures and error streaks
    handshake_raw = bridge.get("handshake", {})
    if isinstance(handshake_raw, dict) and handshake_raw:
        handshake = cast(dict[str, object], handshake_raw)
        last_error: str = str(handshake.get("last_error", ""))
        if last_error:
            errors.append(f"Handshake reported error: {last_error}")

        failures_raw = handshake.get("failures", 0)
        failures: int = int(cast(int, failures_raw)) if isinstance(failures_raw, (int, float)) else 0
        if failures > 0:
            errors.append(f"Handshake reported {failures} failure(s)")

        streak_raw = handshake.get("failure_streak", 0)
        failure_streak: int = int(cast(int, streak_raw)) if isinstance(streak_raw, (int, float)) else 0
        if failure_streak > 0:
            errors.append(f"Handshake failure streak active (streak={failure_streak})")

        fatal_raw = handshake.get("fatal_count", 0)
        fatal_count: int = int(cast(int, fatal_raw)) if isinstance(fatal_raw, (int, float)) else 0
        if fatal_count > 0:
            reason: str = str(handshake.get("fatal_reason", "unknown"))
            detail: str = str(handshake.get("fatal_detail", ""))
            errors.append(f"Fatal handshake count={fatal_count} (reason={reason}, detail={detail})")

    return errors


@app.command()
def audit(
    status_path: Annotated[
        str | None,
        typer.Option("--status-path", "-p", help="Path to mcubridge_status.json"),
    ] = None,
    raw_json: Annotated[
        str | None,
        typer.Option("--raw-json", "-j", help="Raw JSON string to audit"),
    ] = None,
) -> None:
    """Audit status file or raw JSON content."""
    json_text: str = ""

    if raw_json:
        json_text = raw_json
    elif status_path:
        p = Path(status_path)
        if not p.exists():
            print(f"❌ [STATUS AUDIT FAIL] Status file not found: {status_path}", file=sys.stderr)
            sys.exit(1)
        json_text = p.read_text(encoding="utf-8")
    else:
        # Default to reading /tmp/mcubridge_status.json or stdin
        default_path = Path("/tmp/mcubridge_status.json")
        if default_path.exists():
            json_text = default_path.read_text(encoding="utf-8")
        elif not sys.stdin.isatty():
            json_text = sys.stdin.read()
        else:
            print(
                "❌ [STATUS AUDIT FAIL] No status file provided and /tmp/mcubridge_status.json does not exist",
                file=sys.stderr,
            )
            sys.exit(1)

    try:
        data = json.loads(json_text)
    except Exception as e:
        print(f"❌ [STATUS AUDIT FAIL] Failed to parse status JSON: {e}", file=sys.stderr)
        sys.exit(1)

    errors = audit_status_dict(data)
    if errors:
        print("❌ [STATUS AUDIT FAIL] Status snapshot contains active errors:", file=sys.stderr)
        for err in errors:
            print(f"   • {err}", file=sys.stderr)
        sys.exit(1)

    print("✅ [STATUS AUDIT PASS] Status snapshot is clean and healthy (0 errors).")
    sys.exit(0)


if __name__ == "__main__":
    app()
