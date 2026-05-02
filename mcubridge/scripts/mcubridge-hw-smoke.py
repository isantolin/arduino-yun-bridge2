#!/usr/bin/env python3
"""Modernized Hardware Smoke Test for MCU Bridge (SIL-2)."""

from __future__ import annotations

import asyncio
import argparse
from typing import Any
import aiomqtt
from mcubridge.config.settings import load_runtime_config


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
                print(
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
                            print(f"[+] Version received: {payload_str}")
                            self.results["connectivity"] = True
                            break
                except asyncio.TimeoutError:
                    print("[-] Timeout waiting for version response.")
                    self.results["connectivity"] = False

                if not self.results.get("connectivity"):
                    return

                # 2. GPIO Toggle
                print(f"[*] Toggling Pin {pin}...")
                digital_topic = f"{self.prefix}/d/{pin}"
                await client.publish(digital_topic, payload=b"1")
                await asyncio.sleep(0.5)
                await client.publish(digital_topic, payload=b"0")
                self.results["gpio"] = True
                print(f"[+] Pin {pin} toggled.")

        except (aiomqtt.MqttError, OSError, RuntimeError) as e:
            print(f"[!] MQTT Error: {e}")
            self.results["connectivity"] = False


def main() -> None:
    """Execute a suite of hardware diagnostic tests via MQTT."""
    parser = argparse.ArgumentParser(
        description="Diagnostic smoke test for MCU hardware."
    )
    parser.add_argument("--pin", type=int, default=13, help="Pin to toggle during test")
    parser.add_argument(
        "--timeout", type=float, default=5.0, help="Timeout for responses"
    )
    args = parser.parse_args()

    tester = SmokeTester()
    asyncio.run(tester.run(args.pin, args.timeout))

    success = all(tester.results.values()) and bool(tester.results)
    if success:
        print("\n[PASS] Hardware smoke test successful.")
    else:
        print("\n[FAIL] Hardware smoke test failed.")
        import sys

        sys.exit(1)


if __name__ == "__main__":
    main()
