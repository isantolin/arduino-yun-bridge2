"""MQTT publisher helper for synchronous clients (CGI scripts)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from yunbridge.config.settings import RuntimeConfig


def publish_with_retries(
    topic: str,
    payload: str | bytes,
    config: RuntimeConfig,
    logger: logging.Logger,
    retries: int,
    publish_timeout: float,
    base_delay: float,
    sleep_fn: Callable[[float], None],
    poll_interval: float,
    tls_material: Any,
    client_module: Any,
) -> None:
    """Publish a message to MQTT with retries using a synchronous client."""
    
    last_error: Exception | None = None
    
    for attempt in range(1, retries + 1):
        client = client_module.Client(
            client_id=f"yunbridge_cgi_{time.time()}",
            protocol=client_module.MQTTv5,
        )
        
        if tls_material:
            client.tls_set(
                ca_certs=tls_material.cafile,
                certfile=tls_material.certfile,
                keyfile=tls_material.keyfile,
            )

        if config.mqtt_user:
            client.username_pw_set(config.mqtt_user, config.mqtt_pass)

        try:
            client.connect(config.mqtt_host, config.mqtt_port, keepalive=10)
            client.loop_start()
            
            info = client.publish(topic, payload, qos=1)
            
            start_time = time.time()
            while not info.is_published():
                if time.time() - start_time > publish_timeout:
                    raise TimeoutError("Publish timed out")
                sleep_fn(poll_interval)
                
            client.loop_stop()
            client.disconnect()
            return

        except Exception as exc:
            last_error = exc
            logger.warning(
                "Publish attempt %d/%d failed: %s", 
                attempt, retries, exc
            )
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
            
            if attempt < retries:
                sleep_fn(base_delay * (2 ** (attempt - 1)))

    raise RuntimeError(f"Failed to publish after {retries} attempts") from last_error
