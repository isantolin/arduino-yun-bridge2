#!/usr/bin/env python3
"""Rotate McuBridge shared credentials stored in UCI and restart the daemon.

[SIL-2] Improved robustness using Python 'sh' library and strict validation.
"""

from __future__ import annotations

import sh
import uci
import binascii
import os
import sys
import shutil
import tenacity
import typer
from pathlib import Path

app = typer.Typer(add_completion=False)


def get_uci_value(u: uci.Uci, section: str, option: str, default: str = "") -> str:
    """Helper to safely get UCI value using the native binding."""
    try:
        return u.get("mcubridge", section, option)
    except (uci.UciException, RuntimeError):
        return default


def set_uci_value(u: uci.Uci, section: str, option: str, value: str) -> None:
    """Helper to safely set UCI value using the native binding."""
    try:
        u.set("mcubridge", section, option, value)
    except (uci.UciException, RuntimeError) as e:
        sys.stderr.write(f"[mcubridge-rotate-credentials] ERROR: Failed to set {section}.{option}: {e}\n")
        raise


def random_hex(length: int) -> str:
    return binascii.hexlify(os.urandom(length)).decode()


def random_b64(length: int) -> str:
    import base64
    return base64.b64encode(os.urandom(length)).decode().rstrip("=")


@app.command()
def main() -> None:
    # Ensure running as root for UCI commit and service restart
    if os.geteuid() != 0:
        sys.stderr.write("[mcubridge-rotate-credentials] ERROR: Run as root.\n")
        raise typer.Exit(code=1)

    uci_config = Path("/etc/config/mcubridge")
    backup_config = Path("/tmp/mcubridge_config_backup")

    u = uci.Uci()

    try:
        # 0. Backup existing config if it exists
        if uci_config.exists():
            shutil.copy2(uci_config, backup_config)

        # 1. Generate new secrets
        serial_secret = random_hex(32)
        mqtt_pass = random_b64(32)

        # 2. Get existing MQTT user or default
        mqtt_user = get_uci_value(u, "general", "mqtt_user", "mcubridge")

        # 3. Update UCI configuration
        set_uci_value(u, "general", "serial_shared_secret", serial_secret)
        set_uci_value(u, "general", "mqtt_user", mqtt_user)
        set_uci_value(u, "general", "mqtt_pass", mqtt_pass)
        u.commit("mcubridge")

        # 4. Restart service if init script exists
        init_script = Path("/etc/init.d/mcubridge")
        if init_script.exists():
            @tenacity.retry(
                stop=tenacity.stop_after_attempt(3),
                wait=tenacity.wait_fixed(1.0),
                retry=tenacity.retry_if_exception_type(sh.ErrorReturnCode),
                reraise=True
            )
            def restart_service():
                sh.Command(str(init_script))("restart")

            try:
                restart_service()
            except sh.ErrorReturnCode as e:
                sys.stderr.write(f"[mcubridge-rotate-credentials] WARNING: Service restart failed: {e}\n")
                # ROLLBACK: Restore config if restart fails critically
                if backup_config.exists():
                    sys.stderr.write("[mcubridge-rotate-credentials] INFO: Rolling back UCI configuration...\n")
                    shutil.copy2(backup_config, uci_config)
                    raise typer.Exit(code=1)

        # 5. Output results to a restricted-permission file (not stdout/logs)
        secret_file = Path("/tmp/mcubridge_rotated_secret")
        secret_file.write_text(f"SERIAL_SECRET={serial_secret}\n", encoding="utf-8")
        os.chmod(secret_file, 0o600)
        sys.stdout.write(f"SECRET_FILE={secret_file}\n")
        sys.stderr.write("[mcubridge-rotate-credentials] Updated UCI credentials and restarted McuBridge.\n")

    except (sh.ErrorReturnCode, uci.UciException, RuntimeError) as exc:
        if isinstance(exc, sh.ErrorReturnCode):
            msg = exc.stderr.decode()
        else:
            msg = str(exc)
        sys.stderr.write(f"[mcubridge-rotate-credentials] ERROR: UCI or Service operation failed: {msg}\n")
        if backup_config.exists():
            shutil.copy2(backup_config, uci_config)
        raise typer.Exit(code=1)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"[mcubridge-rotate-credentials] FATAL ERROR: {exc}\n")
        if backup_config.exists():
            shutil.copy2(backup_config, uci_config)
        raise typer.Exit(code=1)
    finally:
        if backup_config.exists():
            backup_config.unlink()

if __name__ == "__main__":
    app()
