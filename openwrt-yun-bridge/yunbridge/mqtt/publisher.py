"""Synchronous MQTT publishing helpers using asyncio/aiomqtt internally.

Modernized replacement for the old paho-mqtt synchronous wrapper.
This module provides a synchronous entry point (`publish_with_retries`)
that spins up a temporary asyncio event loop to perform a robust,
retrying publish using the modern aiomqtt library.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
import time

import aiomqtt
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config.settings import RuntimeConfig
from ..config.tls import resolve_tls_material

logger = logging.getLogger(__name__)


async def _publish_async(
    topic: str,
    payload: str | bytes,
    config: RuntimeConfig,
    retries: int = 3,
    timeout: float = 5.0,
    base_delay: float = 0.5,
) -> None:
    """Async implementation of the publishing logic using aiomqtt."""
    
    tls_params = None
    if config.mqtt_tls:
        material = resolve_tls_material(config)
        # aiomqtt handles TLS context creation if given params
        # We construct a TLSContext-like dict or pass SSLContext directly
        # For simplicity here, we assume standard TLS context creation
        import ssl
        tls_context = ssl.create_default_context(
            purpose=ssl.Purpose.SERVER_AUTH,
            cafile=material.cafile
        )
        if material.certfile and material.keyfile:
            tls_context.load_cert_chain(material.certfile, material.keyfile)
        tls_params = tls_context

    retryer = AsyncRetrying(
        reraise=True,
        stop=stop_after_attempt(retries),
        wait=wait_exponential(multiplier=base_delay, min=base_delay, max=5.0),
        retry=retry_if_exception_type(Exception),
    )

    async for attempt in retryer:
        with attempt:
            try:
                async with aiomqtt.Client(
                    hostname=config.mqtt_host,
                    port=config.mqtt_port,
                    username=config.mqtt_user,
                    password=config.mqtt_pass,
                    tls_context=tls_params,
                    timeout=timeout,
                ) as client:
                    await client.publish(topic, payload, qos=1)
                    logger.info("Published to %s (len=%d)", topic, len(payload))
            except Exception as exc:
                logger.warning(
                    "MQTT publish attempt failed: %s", exc
                )
                raise


def publish_with_retries(
    topic: str,
    payload: str | bytes,
    config: RuntimeConfig,
    *,
    logger: logging.Logger = logger,
    retries: int = 3,
    publish_timeout: float = 5.0,
    base_delay: float = 0.5,
    # Legacy arguments ignored for compatibility
    sleep_fn: Callable[[float], None] | None = None,
    poll_interval: float = 0.05,
    tls_material: Any | None = None,
    client_module: Any | None = None,
) -> None:
    """Publish payload to topic using a temporary async loop.
    
    This function replaces the old paho-mqtt loop handling with a 
    clean asyncio.run() call wrapping aiomqtt.
    """
    
    # Ensure payload is bytes or str
    if not isinstance(payload, (str, bytes)):
        payload = str(payload)

    try:
        asyncio.run(
            _publish_async(
                topic,
                payload,
                config,
                retries=retries,
                timeout=publish_timeout,
                base_delay=base_delay,
            )
        )
    except Exception as exc:
        logger.error("Failed to publish message after retries: %s", exc)
        # We swallow the error to mimic legacy fire-and-forget behavior 
        # unless strict error handling is required, but usually scripts 
        # just log and exit.

__all__ = [
    "publish_with_retries",
]