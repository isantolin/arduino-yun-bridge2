#!/usr/bin/env python3
"""Symmetry Linter: Verifies that all commands in spec.toml have C++ handlers."""

import re
import sys
from pathlib import Path
import tomllib


# Commands that intentionally use _unusedCommandSlot in the jump tables.
# Status codes, mcu-only originations, and reserved gaps don't need real handlers.
_IGNORED_COMMANDS: frozenset[str] = frozenset()


def _command_to_handler(name: str) -> str:
    """Convert a spec.toml command name to the expected C++ handler name.

    CMD_GET_VERSION      → _handleGetVersion
    CMD_LINK_SYNC_RESP   → _handleLinkSyncResp
    CMD_SET_PIN_MODE     → _handleSetPinMode
    CMD_DIGITAL_WRITE    → _handleDigitalWrite
    CMD_SPI_SET_CONFIG   → _handleSpiSetConfig
    CMD_ENTER_BOOTLOADER → _handleEnterBootloader
    """
    # Strip CMD_ prefix, split by underscore, title-case each part.
    raw = name.removeprefix("CMD_")
    parts = raw.split("_")
    return "_handle" + "".join(p.capitalize() for p in parts)


def check_symmetry() -> int:
    root = Path(__file__).parent.parent
    spec_path = root / "tools/protocol/spec.toml"
    bridge_cpp = root / "mcubridge-library-arduino/src/Bridge.cpp"

    with open(spec_path, "rb") as f:
        spec = tomllib.load(f)

    with open(bridge_cpp, "r") as f:
        cpp_content = f.read()

    # Collect all handler references from jump tables (BridgeClass::_handleFoo).
    handler_pattern = re.compile(r"&BridgeClass::(_handle\w+)")
    cpp_handlers = set(handler_pattern.findall(cpp_content))

    # Parse commands that flow *to* the MCU (linux_to_mcu direction).
    commands = spec.get("commands", [])
    mcu_inbound = [
        cmd for cmd in commands
        if "linux_to_mcu" in cmd.get("directions", [])
        and cmd["name"] not in _IGNORED_COMMANDS
    ]

    missing: list[str] = []
    for cmd in mcu_inbound:
        expected = _command_to_handler(cmd["name"])
        if expected not in cpp_handlers:
            missing.append(f"  {cmd['name']} (0x{cmd['value']:02X}) → {expected}")

    if missing:
        sys.stderr.write(
            f"ERROR: {len(missing)} MCU-inbound command(s) without C++ jump-table handler:\n"
        )
        for line in missing:
            sys.stderr.write(line + "\n")
        return 1

    sys.stdout.write(
        f"Symmetry check PASSED: {len(mcu_inbound)} MCU-inbound commands "
        f"verified against {len(cpp_handlers)} C++ handlers.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(check_symmetry())
