#!/usr/bin/env python3
"""Modernized Credential Rotation utility for MCU Bridge (SIL-2)."""

from __future__ import annotations

import secrets
import subprocess
import sys
from typing import Annotated

import typer
import uci

app = typer.Typer(add_completion=False, help="Rotate MCU Bridge shared secret.")


def update_uci_secret(new_secret: str) -> None:
    """Update mcubridge.general.secret in UCI."""
    try:
        u = uci.Uci()
        u.set("mcubridge", "general", "secret", new_secret)
        u.commit("mcubridge")
    except (uci.UciException, RuntimeError) as e:
        sys.stderr.write(f"Error: Failed to update UCI: {e}\n")
        raise typer.Exit(code=3)


def restart_service() -> None:
    """Restart the mcubridge service to apply new credentials."""
    try:
        subprocess.run(
            ["/etc/init.d/mcubridge", "restart"], check=True, capture_output=True
        )
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"Warning: Service restart failed: {e.stderr.decode()}\n")


@app.command()
def main(
    length: Annotated[
        int, typer.Option(help="Length of the random secret in bytes")
    ] = 32,
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Force rotation without confirmation")
    ] = False,
    no_restart: Annotated[
        bool, typer.Option("--no-restart", help="Skip service restart")
    ] = False,
) -> None:
    """Generate and apply a new shared secret for the MCU Bridge."""
    if not force:
        typer.confirm(
            "This will rotate the shared secret and may drop MCU connections. Continue?",
            abort=True,
        )

    new_secret = secrets.token_hex(length)
    typer.echo(f"Generated new secret: {new_secret[:4]}...{new_secret[-4:]}")

    update_uci_secret(new_secret)
    typer.echo("UCI configuration updated.")

    if not no_restart:
        typer.echo("Restarting service...")
        restart_service()
        typer.echo("Service restarted.")


if __name__ == "__main__":
    app()
