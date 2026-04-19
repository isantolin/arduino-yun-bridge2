#!/usr/bin/env python3
"""Exercise datastore interactions using direct MQTT."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import typer
from mcubridge_client import Topic
from mcubridge_client.cli import bridge_session, configure_logging

app = typer.Typer(help="Exercise datastore interactions using direct MQTT.")
configure_logging()


async def mqtt_get(client, key: str) -> str:
    get_topic = str(Topic.build(Topic.DATASTORE, "get", key))
    req_topic = str(Topic.build(Topic.DATASTORE, "get", key, "request"))
    
    await client.subscribe(get_topic)
    await client.publish(req_topic, b"")
    
    try:
        async with asyncio.timeout(5.0):
            async for message in client.messages:
                if Topic.matches(get_topic, str(message.topic)):
                    return message.payload.decode()
    except asyncio.TimeoutError:
        return ""
    finally:
        await client.unsubscribe(get_topic)
    return ""


async def run_test(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    tls_insecure: bool,
) -> None:

    async with bridge_session(host, port, user, password, tls_insecure) as client:
        logging.info("--- Starting DataStore Direct MQTT Test ---")

        # --- Test 1: Put and Get a new key-value pair ---
        logging.info("[Test 1: Put and Get a new key-value pair]")
        key1: str = "client_test/temperature"
        value1: str = "25.5"

        put_topic = str(Topic.build(Topic.DATASTORE, "put", key1))
        await client.publish(put_topic, value1.encode())
        logging.info(f"Published value '{value1}' to topic '{put_topic}'")

        await asyncio.sleep(0.5) # Give daemon time to process

        retrieved_value = await mqtt_get(client, key1)
        if retrieved_value == value1:
            logging.info(
                "SUCCESS: Retrieved value '%s' matches put value '%s'.",
                retrieved_value,
                value1,
            )
        else:
            logging.error(
                "FAILURE: Retrieved value '%s' does not match put value '%s'.",
                retrieved_value,
                value1,
            )

        # --- Test 2: Get a non-existent key ---
        logging.info("\n[Test 2: Get a non-existent key]")
        key2: str = "non_existent/key"

        retrieved_value_2 = await mqtt_get(client, key2)
        # Expecting an empty payload for a non-existent key
        if retrieved_value_2 == "":
            logging.info(
                "SUCCESS: Empty value returned for non-existent key '%s'.",
                key2,
            )
        else:
            logging.error(
                "FAILURE: Value '%s' returned for non-existent key '%s'.",
                retrieved_value_2,
                key2,
            )

    logging.info("Done.")


@app.command()
def main(
    host: Annotated[str | None, typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[int | None, typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[str | None, typer.Option(help="MQTT Username")] = None,
    password: Annotated[str | None, typer.Option(help="MQTT Password")] = None,
    tls_insecure: Annotated[
        bool, typer.Option(help="Disable TLS certificate verification")
    ] = False,
) -> None:
    asyncio.run(run_test(host, port, user, password, tls_insecure))


if __name__ == "__main__":
    app()
