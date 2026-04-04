#!/usr/bin/env python3
import logging
import paho.mqtt.publish as publish
import time


def trigger_bootloader():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger("bootloader_sim")

    topic = "br/system/bootloader"
    broker = "localhost"

    # Handshake completo toma ~20s en el emulador
    log.info("Waiting 30s for Full Link Synchronization...")
    time.sleep(30)

    log.info(f"Triggering bootloader via MQTT topic: {topic}")
    publish.single(topic, b"", hostname=broker, qos=1)

    log.info("Message published. Watching for MCU output (5s)...")
    time.sleep(5)

if __name__ == "__main__":
    trigger_bootloader()
