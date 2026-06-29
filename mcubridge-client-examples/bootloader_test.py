#!/usr/bin/env python3
import logging
import asyncio
import aiomqtt
import time


async def _publish(topic: str, broker: str) -> None:
    async with aiomqtt.Client(hostname=broker) as client:
        await client.publish(topic, b"", qos=1)


def trigger_bootloader() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger("bootloader_sim")

    topic = "br/system/bootloader"
    broker = "localhost"

    # Handshake completo toma ~20s en el emulador
    log.info("Waiting 30s for Full Link Synchronization...")
    time.sleep(30)

    log.info(f"Triggering bootloader via MQTT topic: {topic}")
    asyncio.run(_publish(topic, broker))

    log.info("Message published. Watching for MCU output (5s)...")
    time.sleep(5)


if __name__ == "__main__":
    trigger_bootloader()
