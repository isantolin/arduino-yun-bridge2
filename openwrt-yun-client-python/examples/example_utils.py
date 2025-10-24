"""Utilities for the Yun Bridge examples, using an async MQTT client."""
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import aiomqtt

# Centralized MQTT configuration for all examples
# The broker IP can be overridden with an environment variable
MQTT_HOST = os.environ.get("YUN_BROKER_IP", "192.168.15.28")
MQTT_PORT = int(os.environ.get("YUN_BROKER_PORT", 1883))


@asynccontextmanager
async def get_mqtt_client() -> AsyncGenerator[aiomqtt.Client, None]:
    """An async context manager to simplify aiomqtt.Client setup and connection."""
    try:
        async with aiomqtt.Client(hostname=MQTT_HOST, port=MQTT_PORT) as client:
            logging.info("Connected to MQTT broker at %s:%s.", MQTT_HOST, MQTT_PORT)
            yield client
    except aiomqtt.MqttError as error:
        logging.error("Error connecting to MQTT broker: %s", error)
        # Re-raise the exception to be handled by the caller
        raise
    finally:
        logging.info("Disconnected from MQTT broker.")
