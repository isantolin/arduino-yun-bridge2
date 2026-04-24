#!/usr/bin/env python3
"""Modernized Hardware Smoke Test for MCU Bridge (SIL-2)."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import aiomqtt
import typer
from mcubridge.config.settings import load_runtime_config

app = typer.Typer(add_completion=False, help="Diagnostic smoke test for MCU hardware.")


class SmokeTester:
    def __init__(self) -> None:
        self.config = load_runtime_config()
        self.prefix = self.config.mqtt_topic
        self.results: dict[str, bool] = {}

    async def run(self, pin: int, timeout: float) -> None:
        tls_context = self.config.get_ssl_context()

        try:
            async with aiomqtt.Client(
                hostname=self.config.mqtt_host,
                port=self.config.mqtt_port,
                username=self.config.mqtt_user or None,
                password=self.config.mqtt_pass or None,
                tls_context=tls_context,
            ) as client:
                typer.echo(
                    f"[*] Testing MCU Bridge on {self.config.mqtt_host}:{self.config.mqtt_port}"
                )

                # 1. Connectivity & Version
                version_topic = f"{self.prefix}/system/version/get"
                resp_topic = f"{self.prefix}/system/version/response"

                await client.subscribe(resp_topic)
                await client.publish(version_topic, payload=b"")

                try:
                    async with asyncio.timeout(timeout):
                        async for msg in client.messages:
                            payload_raw: Any = msg.payload
                            payload_str = (
                                payload_raw.decode()
                                if isinstance(payload_raw, bytes)
                                else str(payload_raw)
                            )
                            typer.echo(f"[+] Version received: {payload_str}")
                            self.results["connectivity"] = True
                            break
                except asyncio.TimeoutError:
                    typer.echo("[-] Timeout waiting for version response.")
                    self.results["connectivity"] = False

                if not self.results.get("connectivity"):
                    return

                # 2. GPIO Toggle
                typer.echo(f"[*] Toggling Pin {pin}...")
                digital_topic = f"{self.prefix}/d/{pin}"
                await client.publish(digital_topic, payload=b"1")
                await asyncio.sleep(0.5)
                await client.publish(digital_topic, payload=b"0")
                self.results["gpio"] = True
                typer.echo(f"[+] Pin {pin} toggled.")

        except (aiomqtt.MqttError, OSError, RuntimeError) as e:
            typer.echo(f"[!] MQTT Error: {e}", err=True)
            self.results["connectivity"] = False


@app.command()
def main(
    pin: Annotated[int, typer.Option(help="Pin to toggle during test")] = 13,
    timeout: Annotated[float, typer.Option(help="Timeout for responses")] = 5.0,
) -> None:
    """Execute a suite of hardware diagnostic tests via MQTT."""
    tester = SmokeTester()
    asyncio.run(tester.run(pin, timeout))

    success = all(tester.results.values()) and bool(tester.results)
    if success:
        typer.echo("\n[PASS] Hardware smoke test successful.")
    else:
        typer.echo("\n[FAIL] Hardware smoke test failed.")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
