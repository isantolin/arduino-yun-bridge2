#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] Hardware smoke test utility for MCU Bridge ecosystem.

Verifies local OpenWrt UBUS IPC connectivity, round-trip latency, link state,
and clock synchronization against the running McuBridge daemon.
"""

from __future__ import annotations

import importlib
import json
import sys
import time
from typing import Annotated, Any, cast

import structlog
import typer

logger = structlog.get_logger("mcubridge.hw-smoke")
app = typer.Typer(help="Execute hardware smoke tests for McuBridge.", add_completion=False)

try:
    ubus: Any = importlib.import_module("ubus")
except (ImportError, TypeError) as exc:
    logger.debug("Native OpenWrt UBUS module unavailable", error=str(exc))
    ubus = None


def ubus_call(
    obj: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    ubus_module: Any = None,
) -> dict[str, Any] | None:
    """Call a UBUS method directly via the native OpenWrt python-ubus library API. [Rule 37]"""
    ubus_mod = ubus_module if ubus_module is not None else ubus
    if ubus_mod is None:
        logger.error("Native OpenWrt python-ubus library is required for hardware smoke tests")
        return None
    payload = params if params is not None else {}
    try:
        if ubus_mod.connect() is not True:
            raise OSError("ubus.connect() did not return True")
        res: Any = ubus_mod.call(obj, method, payload)
        first: Any = res[0] if isinstance(res, list) and res else res
        if isinstance(first, dict):
            return cast(dict[str, Any], first)
        if res is not None:
            return {"result": res}
    except (OSError, RuntimeError, AttributeError) as exc:
        logger.error("python-ubus call failed", method=method, error=str(exc))
    return None


@app.command()
def smoke_test(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose JSON output")] = False,
    timeout: Annotated[float, typer.Option("--timeout", "-t", help="Timeout per check in seconds")] = 5.0,
) -> None:
    """Run comprehensive hardware smoke test suite across UBUS and MCU link."""
    print("========================================")
    print("McuBridge Hardware Smoke Test Suite")
    print("========================================")

    all_passed = True
    results: dict[str, Any] = {}

    # Check 1: Ping & Latency
    print("[1/3] Testing UBUS IPC & Ping...")
    start_time = time.monotonic()
    ping_res = ubus_call("mcubridge", "ping")
    elapsed_ms = (time.monotonic() - start_time) * 1000.0

    if ping_res is not None and ping_res.get("status") == "ok" and elapsed_ms <= (timeout * 1000.0):
        print(f"      PASS: Ping replied in {elapsed_ms:.2f} ms (status: ok)")
        results["ping"] = {"passed": True, "latency_ms": round(elapsed_ms, 2), "response": ping_res}
    else:
        print(f"      FAIL: Ping failed or timed out (response: {ping_res})")
        results["ping"] = {"passed": False, "response": ping_res}
        all_passed = False

    # Check 2: Daemon Status & Link FSM
    print("[2/3] Checking Bridge Status & FSM State...")
    status_res = ubus_call("mcubridge", "status")
    if status_res is not None:
        state_str = status_res.get("state") or status_res.get("connection_state", "UNKNOWN")
        synced = bool(status_res.get("synchronized", False))
        port = status_res.get("port", "unknown")
        baud = status_res.get("baudrate", 0)
        print(f"      PASS: State: {state_str} | Synced: {synced} | Port: {port}@{baud}")
        results["status"] = {"passed": True, "state": state_str, "synced": synced, "details": status_res}
    else:
        print("      FAIL: Failed to query status from mcubridge UBUS service.")
        results["status"] = {"passed": False}
        all_passed = False

    # Check 3: Clock Synchronization Status
    print("[3/3] Checking Clock Synchronization Status...")
    clock_res = ubus_call("mcubridge", "clock_status")
    if clock_res is not None:
        clock_synced = bool(clock_res.get("synchronized", False))
        drift_ms = clock_res.get("drift_ms", 0.0)
        print(f"      PASS: Clock Synchronized: {clock_synced} | Drift: {drift_ms} ms")
        results["clock"] = {"passed": True, "synced": clock_synced, "drift_ms": drift_ms}
    else:
        print("      FAIL: Failed to query clock_status from mcubridge UBUS service.")
        results["clock"] = {"passed": False}
        all_passed = False

    print("----------------------------------------")
    if verbose:
        print(json.dumps(results, indent=2))
        print("----------------------------------------")

    if all_passed:
        print("RESULT: SUCCESS - All hardware smoke checks passed cleanly.")
        sys.exit(0)
    else:
        print("RESULT: FAILURE - One or more hardware smoke checks failed.")
        sys.exit(1)


if __name__ == "__main__":
    app()
