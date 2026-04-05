#!/usr/bin/env python3
"""Simple script to control MCU pins (like LED 13) from OpenWrt via MQTT.

Usage: mcubridge-led-control on|off [pin]
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import paho.mqtt.publish as publish
import tenacity
import typer
import uci

if TYPE_CHECKING:
    from paho.mqtt.publish import AuthParameter, TLSParameter

app = typer.Typer(add_completion=False)

# Configure syslog logging
logger = logging.getLogger("mcubridge-led")
logger.setLevel(logging.INFO)
syslog_handler = logging.handlers.SysLogHandler(address="/dev/log")
syslog_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
logger.addHandler(syslog_handler)


def uci_get(key: str, default: str = "") -> str:
    try:
        u = uci.Uci()
        return u.get("mcubridge", "general", key)
    except (uci.UciException, RuntimeError):
        return default


@app.command()
def main(
    state_arg: Annotated[str, typer.Argument(help="State to set the LED to (on/off)")],
    pin: Annotated[int, typer.Argument(help="Pin number to control")] = 13
) -> None:
    state_arg = state_arg.lower()
    if state_arg not in ("on", "off"):
        sys.stderr.write(f"Error: invalid state '{state_arg}'. Use on|off.\n")
        raise typer.Exit(code=2)

    payload = "1" if state_arg == "on" else "0"

    topic_prefix = uci_get("mqtt_topic", "br")
    mqtt_topic = f"{topic_prefix}/d/{pin}"

    # MQTT Config
    host = uci_get("mqtt_host", "127.0.0.1")
    port = int(uci_get("mqtt_port", "1883"))
    user = uci_get("mqtt_user") or None
    pw = uci_get("mqtt_pass") or None

    auth: AuthParameter | None = None
    if user:
        auth = {"username": user}
        if pw:
            auth["password"] = pw

    tls_config: TLSParameter | None = None
    if uci_get("mqtt_tls", "1") == "1":
        cafile = uci_get("mqtt_cafile")
        if not cafile or not Path(cafile).exists():
            fallback_ca = Path("/etc/ssl/certs/ca-certificates.crt")
            cafile = str(fallback_ca) if fallback_ca.exists() else ""

        insecure = uci_get("mqtt_tls_insecure", "0") == "1"

        tls_dict: dict[str, str | bool] = {"insecure": insecure}
        if cafile:
            tls_dict["ca_certs"] = cafile
        tls_config = cast(TLSParameter, tls_dict)

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_fixed(0.5),
        retry=tenacity.retry_if_exception_type(Exception),
        reraise=True
    )
    def do_publish():
        publish.single(
            mqtt_topic,
            payload=payload,
            hostname=host,
            port=port,
            auth=auth,
            tls=tls_config
        )
        logger.info(f"Published LED{pin}={payload} to {mqtt_topic}")

    try:
        do_publish()
    except Exception as e:
        logger.error(f"Failed to publish after retries: {e}")
        sys.stderr.write(f"Error: failed to publish MQTT message: {e}\n")
        raise typer.Exit(code=4)

if __name__ == "__main__":
    app()
