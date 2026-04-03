#!/usr/bin/env python3
"""Run a basic McuBridge hardware smoke test locally on the device.

[SIL-2] Improved robustness using native aiomqtt and structured validation.
"""

from __future__ import annotations

import asyncio
import sys
import ssl
import subprocess
from pathlib import Path
from typing import Any, cast

import aiomqtt
import tenacity
import typer
import uci

app = typer.Typer(add_completion=False)
STATUS_FILE = Path("/tmp/mcubridge_status.json")

def uci_get(key: str, default: str = "") -> str:
    try:
        u = uci.Uci()
        return u.get("mcubridge", "general", key)
    except (uci.UciException, RuntimeError):
        return default

async def perform_round_trip(
    host: str,
    port: int,
    topic_prefix: str,
    tls_params: dict[str, Any] | None,
    auth_params: dict[str, Any] | None
) -> str:
    status_topic = f"{topic_prefix}/system/status"
    request_topic = f"{topic_prefix}/system/status/request"

    @tenacity.retry(
        stop=tenacity.stop_after_delay(15),
        wait=tenacity.wait_fixed(1.0),
        retry=tenacity.retry_if_exception_type((asyncio.TimeoutError, aiomqtt.MqttError)),
        reraise=True
    )
    async def _round_trip() -> str:
        async with aiomqtt.Client(
            hostname=host,
            port=port,
            username=cast(str, auth_params["username"]) if auth_params else None,
            password=cast(str, auth_params["password"]) if auth_params else None,
            tls_context=cast(ssl.SSLContext, tls_params["context"]) if tls_params else None,
            tls_insecure=cast(bool, tls_params["insecure"]) if tls_params else False
        ) as client:
            await client.subscribe(status_topic)
            await client.publish(request_topic, payload='{"request":"ping"}')

            # Wait for response with timeout
            async with asyncio.timeout(5.0):
                async for message in client.messages:
                    return message.payload.decode()
        raise TimeoutError("No response received from daemon")

    return await _round_trip()

@app.command()
def main() -> None:
    # 1. Configuration Load
    serial_secret = uci_get("serial_shared_secret")
    if not serial_secret or serial_secret == "changeme123":
        sys.stderr.write("[mcubridge-hw-smoke] ERROR: Serial secret missing or insecure.\n")
        raise typer.Exit(code=1)

    host = uci_get("mqtt_host", "127.0.0.1")
    port = int(uci_get("mqtt_port", "1883"))
    topic_prefix = uci_get("mqtt_topic", "br")
    tls_enabled = uci_get("mqtt_tls", "1") == "1"
    user = uci_get("mqtt_user") or None
    pw = uci_get("mqtt_pass") or None
    cafile = uci_get("mqtt_cafile")
    insecure = uci_get("mqtt_tls_insecure", "0") == "1"

    # 2. Environment Check
    init_script = Path("/etc/init.d/mcubridge")
    if init_script.exists():
        res = subprocess.run([str(init_script), "status"], capture_output=True, check=False)
        if res.returncode != 0:
            sys.stderr.write("[mcubridge-hw-smoke] ERROR: McuBridge service is not running.\n")
            raise typer.Exit(code=1)

    if not STATUS_FILE.exists() or STATUS_FILE.stat().st_size == 0:
        sys.stderr.write(f"[mcubridge-hw-smoke] ERROR: Status file {STATUS_FILE} missing or empty.\n")
        raise typer.Exit(code=1)

    # 3. Prepare MQTT Parameters
    auth_params = {"username": user, "password": pw} if user else None

    tls_params = None
    if tls_enabled:
        if not cafile or not Path(cafile).exists():
            fallback_ca = Path("/etc/ssl/certs/ca-certificates.crt")
            cafile = str(fallback_ca) if fallback_ca.exists() else None

        ctx = ssl.create_default_context(cafile=cafile)
        ctx.check_hostname = not insecure
        if insecure:
            ctx.verify_mode = ssl.CERT_NONE

        tls_params = {
            "context": ctx,
            "insecure": insecure
        }

    # 4. Run Smoke Test
    sys.stderr.write(f"[mcubridge-hw-smoke] Checking MQTT round trip via {host}:{port}...\n")

    try:
        response = asyncio.run(perform_round_trip(host, port, topic_prefix, tls_params, auth_params))
        sys.stdout.write("[mcubridge-hw-smoke] Received response:\n")
        sys.stdout.write(response + "\n")
        sys.stderr.write("[mcubridge-hw-smoke] Smoke test completed successfully.\n")

    except (asyncio.TimeoutError, TimeoutError):
        sys.stderr.write("[mcubridge-hw-smoke] ERROR: Did not receive response from daemon (Timeout).\n")
        raise typer.Exit(code=1)
    except (aiomqtt.MqttError, OSError, ValueError) as e:
        sys.stderr.write(f"[mcubridge-hw-smoke] ERROR: System or tool failed: {e}\n")
        raise typer.Exit(code=1)

if __name__ == "__main__":
    app()
