#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] LED / GPIO Control utility for MCU Bridge ecosystem.

Provides deterministic control over MCU hardware GPIO pins (such as LED 13)
via native OpenWrt UBUS IPC, matching LuCI ACL /usr/bin/mcubridge-led-control.
"""

from __future__ import annotations

import importlib
import sys
from typing import Annotated, Any, cast

import structlog
import typer

logger = structlog.get_logger("mcubridge.led-control")
app = typer.Typer(help="Control MCU GPIO LEDs via McuBridge UBUS IPC.", add_completion=False)

try:
    ubus: Any = importlib.import_module("ubus")
except (ImportError, TypeError) as exc:
    logger.debug("Native OpenWrt UBUS module unavailable", error=str(exc))
    ubus = None


def set_digital_pin(pin: int, value: int, *, ubus_module: Any = None) -> bool:
    """Set digital pin state via OpenWrt UBUS IPC. [SIL-2 / Rule 37]"""
    ubus_mod = ubus_module if ubus_module is not None else ubus
    if ubus_mod is None:
        logger.error("Native OpenWrt UBUS module unavailable")
        return False

    payload: dict[str, Any] = {
        "pin": pin,
        "value": 1 if value else 0,
    }

    try:
        if ubus_mod.connect() is not True:
            raise OSError("ubus.connect() did not return True")
        res: Any = ubus_mod.call("mcubridge", "digital_write", payload)
        first: Any = res[0] if isinstance(res, list) and res else res
        if isinstance(first, dict):
            res_dict: dict[str, Any] = cast(dict[str, Any], first)
            if res_dict.get("status") in ("accepted", "ok"):
                return True
        logger.warning("UBUS digital_write replied with non-accepted status", response=res)
        return False
    except (OSError, RuntimeError, AttributeError) as exc:
        logger.error("python-ubus digital_write failed", pin=pin, value=value, error=str(exc))
        return False


@app.command()
def set_pin(
    pin: Annotated[int, typer.Option("--pin", "-p", help="Pin index (default: 13)")] = 13,
    state: Annotated[str, typer.Option("--state", "-s", help="Pin state: ON, OFF, 1, 0")] = "ON",
) -> None:
    """Set the state of an MCU digital pin / LED."""
    normalized = state.strip().upper()
    if normalized in ("ON", "1", "HIGH", "TRUE"):
        val = 1
        name = "ON"
    elif normalized in ("OFF", "0", "LOW", "FALSE"):
        val = 0
        name = "OFF"
    else:
        print(f"Error: Invalid state '{state}'. Expected ON, OFF, 1, or 0.", file=sys.stderr)
        sys.exit(2)

    success = set_digital_pin(pin, val)
    if success:
        print(f"OK: Pin {pin} set to {name} ({val})")
        sys.exit(0)
    else:
        print(f"FAIL: Failed to set pin {pin} to {name} via UBUS", file=sys.stderr)
        sys.exit(1)


@app.command()
def on(
    pin: Annotated[int, typer.Option("--pin", "-p", help="Pin index (default: 13)")] = 13,
) -> None:
    """Turn ON the specified pin (default: LED 13)."""
    set_pin(pin=pin, state="ON")


@app.command()
def off(
    pin: Annotated[int, typer.Option("--pin", "-p", help="Pin index (default: 13)")] = 13,
) -> None:
    """Turn OFF the specified pin (default: LED 13)."""
    set_pin(pin=pin, state="OFF")


if __name__ == "__main__":
    app()
