#!/usr/bin/env python3
"""Modernized Credential Rotation utility for MCU Bridge (SIL-2)."""

from __future__ import annotations
from typing import Annotated

import secrets
import subprocess
import sys
import uci
import structlog

import typer

# [SIL-2] Structured logging towards syslog/stderr
logger = structlog.get_logger("mcubridge.rotate-credentials")
app = typer.Typer(help="Rotate MCU Bridge shared secret.", add_completion=False)


def update_uci_credentials(new_secret: str, new_cloud_password: str) -> None:
    """Update the rotated credentials in mcubridge.general."""
    try:
        u = uci.Uci()
        u.set("mcubridge", "general", "serial_shared_secret", new_secret)
        u.set("mcubridge", "general", "cloud_pass", new_cloud_password)
        u.commit("mcubridge")
        logger.info("UCI configuration updated successfully")
    except (uci.UciException, RuntimeError) as e:
        logger.error("Failed to update UCI", error=str(e))
        sys.exit(3)


def restart_service() -> None:
    """Restart the mcubridge service to apply new credentials."""
    try:
        subprocess.run(["/etc/init.d/mcubridge", "restart"], check=True, capture_output=True)
        logger.info("Bridge service restarted successfully")
    except subprocess.CalledProcessError as e:
        logger.warning("Service restart failed", stderr=e.stderr.decode(), exit_code=e.returncode)


@app.command()
def main(
    length: Annotated[int, typer.Option("--length", help="Length of the random secret in bytes")] = 32,
    force: Annotated[bool, typer.Option("--force", "-f", help="Force rotation without confirmation")] = False,
    no_restart: Annotated[bool, typer.Option("--no-restart", help="Skip service restart")] = False,
) -> None:
    """Generate and apply a new shared secret for the MCU Bridge."""
    if not force:
        sys.stdout.write("This will rotate the shared secret and may drop MCU connections. Continue? [y/N] ")
        sys.stdout.flush()
        ans = sys.stdin.readline()
        if ans.lower().strip() not in ("y", "yes"):
            logger.info("Rotation aborted by user")
            sys.exit(0)

    new_secret = secrets.token_hex(length)
    new_cloud_password = secrets.token_urlsafe(max(24, length))
    # [SIL-2] Sensitive data masked in logs
    masked_secret = f"{new_secret[:4]}...{new_secret[-4:]}"
    logger.info("Generating new shared secret", masked_secret=masked_secret)

    update_uci_credentials(new_secret, new_cloud_password)
    sys.stdout.write(f"SERIAL_SECRET={new_secret}\n")
    sys.stdout.write(f"CLOUD_PASSWORD={new_cloud_password}\n")
    sys.stdout.flush()

    if not no_restart:
        logger.info("Restarting bridge service...")
        restart_service()


if __name__ == "__main__":
    app()
