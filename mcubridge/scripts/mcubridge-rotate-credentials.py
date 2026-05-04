#!/usr/bin/env python3
"""Modernized Credential Rotation utility for MCU Bridge (SIL-2)."""

from __future__ import annotations

import secrets
import subprocess
import sys
import argparse
import uci
import structlog

# [SIL-2] Structured logging towards syslog/stderr
logger = structlog.get_logger("mcubridge.rotate-credentials")


def update_uci_secret(new_secret: str) -> None:
    """Update mcubridge.general.secret in UCI."""
    try:
        u = uci.Uci()
        u.set("mcubridge", "general", "secret", new_secret)
        u.commit("mcubridge")
        logger.info("UCI configuration updated successfully")
    except (uci.UciException, RuntimeError) as e:
        logger.error("Failed to update UCI", error=str(e))
        sys.exit(3)


def restart_service() -> None:
    """Restart the mcubridge service to apply new credentials."""
    try:
        subprocess.run(
            ["/etc/init.d/mcubridge", "restart"], check=True, capture_output=True
        )
        logger.info("Bridge service restarted successfully")
    except subprocess.CalledProcessError as e:
        logger.warning(
            "Service restart failed", stderr=e.stderr.decode(), exit_code=e.returncode
        )


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
        sys.stdout.write(
            "This will rotate the shared secret and may drop MCU connections. Continue? [y/N] "
        )
        sys.stdout.flush()
        ans = sys.stdin.readline()
        if ans.lower().strip() not in ("y", "yes"):
            logger.info("Rotation aborted by user")
            sys.exit(0)

    new_secret = secrets.token_hex(args.length)
    # [SIL-2] Sensitive data masked in logs
    masked_secret = f"{new_secret[:4]}...{new_secret[-4:]}"
    logger.info("Generating new shared secret", masked_secret=masked_secret)

    update_uci_secret(new_secret)

    if not args.no_restart:
        logger.info("Restarting bridge service...")
        restart_service()


if __name__ == "__main__":
    main()
