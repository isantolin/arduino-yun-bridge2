#!/usr/bin/env python3
"""Modernized Credential Rotation utility for MCU Bridge (SIL-2)."""

from __future__ import annotations

import secrets
import subprocess
import sys
import argparse
import uci


def update_uci_secret(new_secret: str) -> None:
    """Update mcubridge.general.secret in UCI."""
    try:
        u = uci.Uci()
        u.set("mcubridge", "general", "secret", new_secret)
        u.commit("mcubridge")
    except (uci.UciException, RuntimeError) as e:
        sys.stderr.write(f"Error: Failed to update UCI: {e}\n")
        sys.exit(3)


def restart_service() -> None:
    """Restart the mcubridge service to apply new credentials."""
    try:
        subprocess.run(
            ["/etc/init.d/mcubridge", "restart"], check=True, capture_output=True
        )
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"Warning: Service restart failed: {e.stderr.decode()}\n")


def main() -> None:
    """Generate and apply a new shared secret for the MCU Bridge."""
    parser = argparse.ArgumentParser(description="Rotate MCU Bridge shared secret.")
    parser.add_argument(
        "--length", type=int, default=32, help="Length of the random secret in bytes"
    )
    parser.add_argument(
        "--force", "-f", action="store_true", help="Force rotation without confirmation"
    )
    parser.add_argument(
        "--no-restart", action="store_true", help="Skip service restart"
    )
    args = parser.parse_args()

    if not args.force:
        ans = input(
            "This will rotate the shared secret and may drop MCU connections. Continue? [y/N] "
        )
        if ans.lower() not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    new_secret = secrets.token_hex(args.length)
    print(f"Generated new secret: {new_secret[:4]}...{new_secret[-4:]}")

    update_uci_secret(new_secret)
    print("UCI configuration updated.")

    if not args.no_restart:
        print("Restarting service...")
        restart_service()
        print("Service restarted.")


if __name__ == "__main__":
    main()
