#!/usr/bin/env python3
"""Audit McuBridge status snapshot for SIL-2 / MIL-SPEC integrity and error-free operation."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

app = typer.Typer(
    help="Audit /tmp/mcubridge_status.json for errors, handshake failures, and metric anomalies.",
    add_completion=False,
)


def audit_status_dict(data: dict[str, Any]) -> list[str]:
    """Inspect status dictionary and return list of error descriptions."""
    errors: list[str] = []

    metrics = data.get("metrics", {})
    if not isinstance(metrics, dict):
        return ["Status 'metrics' field is not a dictionary"]

    # 1. Cloud spool drop / trim anomalies
    if (dropped := metrics.get("cloud_spool_dropped_limit", 0)) > 0:
        errors.append(f"Cloud spool dropped messages limit exceeded (count={dropped})")
    if (trimmed := metrics.get("cloud_spool_trim_events", 0)) > 0:
        errors.append(f"Cloud spool trim events occurred (count={trimmed})")

    bridge = data.get("bridge", {})
    if not isinstance(bridge, dict):
        return ["Status 'bridge' field is not a dictionary"]

    # 2. Serial link state
    serial_link = bridge.get("serial_link", {})
    if isinstance(serial_link, dict) and serial_link:
        if not serial_link.get("connected", False):
            errors.append("Serial link is reported as disconnected")

    # 3. Handshake failures and error streaks
    handshake = bridge.get("handshake", {})
    if isinstance(handshake, dict) and handshake:
        if (last_error := handshake.get("last_error", "")) != "":
            errors.append(f"Handshake reported error: {last_error}")
        if (failures := handshake.get("failures", 0)) > 0:
            errors.append(f"Handshake reported {failures} failure(s)")
        if (failure_streak := handshake.get("failure_streak", 0)) > 0:
            errors.append(f"Handshake failure streak active (streak={failure_streak})")
        if (fatal_count := handshake.get("fatal_count", 0)) > 0:
            reason = handshake.get("fatal_reason", "unknown")
            detail = handshake.get("fatal_detail", "")
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
