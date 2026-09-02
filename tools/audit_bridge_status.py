#!/usr/bin/env python3
"""Audit McuBridge status snapshot for SIL-2 / MIL-SPEC integrity and error-free operation."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

from google.protobuf.json_format import ParseDict
import typer

from mcubridge.protocol import mcubridge_pb2 as pb

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

    bridge_raw = data.get("bridge", {})
    if not isinstance(bridge_raw, dict):
        return ["Status 'bridge' field is not a dictionary"]

    # [SIL-2] Parse into strongly-typed Protobuf BridgeStatus message
    status_pb = pb.BridgeStatus()
    try:
        ParseDict(data, status_pb, ignore_unknown_fields=True)
    except Exception as exc:
        return [f"Status Protobuf deserialization failed: {exc}"]

    # 1. Cloud spool drop / trim anomalies
    dropped = status_pb.metrics.cloud_spool_dropped_limit
    if dropped > 0:
        errors.append(f"Cloud spool dropped messages limit exceeded (count={dropped})")

    trimmed = status_pb.metrics.cloud_spool_trim_events
    if trimmed > 0:
        errors.append(f"Cloud spool trim events occurred (count={trimmed})")

    # 2. Serial link state
    if "serial_link" in bridge_raw:
        if not status_pb.bridge.serial_link.connected:
            errors.append("Serial link is reported as disconnected")

    # 3. Handshake failures and error streaks
    if "handshake" in bridge_raw:
        hs = status_pb.bridge.handshake
        if hs.last_error:
            errors.append(f"Handshake reported error: {hs.last_error}")

        if hs.failures > 0:
            errors.append(f"Handshake reported {hs.failures} failure(s)")

        if hs.failure_streak > 0:
            errors.append(f"Handshake failure streak active (streak={hs.failure_streak})")

        if hs.fatal_count > 0:
            reason = hs.fatal_reason or "unknown"
            detail = hs.fatal_detail or ""
            errors.append(f"Fatal handshake count={hs.fatal_count} (reason={reason}, detail={detail})")

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
