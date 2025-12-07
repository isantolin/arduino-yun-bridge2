"""Synchronous MQTT publishing helpers shared outside the daemon."""
from __future__ import annotations

import logging
import ssl
import time
from collections.abc import Callable
from typing import Any

from paho.mqtt import client as mqtt_client
from tenacity import (
    Retrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config.settings import RuntimeConfig
from ..config.tls import TLSMaterial, resolve_tls_material


def _build_client(
    config: RuntimeConfig,
    *,
    logger: logging.Logger,
    tls_material: TLSMaterial | None = None,
    client_module: Any = mqtt_client,
) -> mqtt_client.Client:
    return build_client(
        config,
        logger=logger,
        tls_material=tls_material,
        client_module=client_module,
    )


def build_client(
    config: RuntimeConfig,
    *,
    logger: logging.Logger,
    tls_material: TLSMaterial | None = None,
    client_module: Any = mqtt_client,
) -> mqtt_client.Client:
    client_cls = getattr(client_module, "Client")
    # paho mqtt 2.0+ uses protocol=MQTTv5 enum or int 5
    client = client_cls(protocol=getattr(client_module, "MQTTv5", 5))
    if config.mqtt_user:
        client.username_pw_set(config.mqtt_user, config.mqtt_pass)

    if config.mqtt_tls:
        material = tls_material or resolve_tls_material(config)
        client.tls_set(
            ca_certs=material.cafile,
            certfile=material.certfile,
            keyfile=material.keyfile,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        client.tls_insecure_set(False)
    else:
        logger.warning(
            "MQTT TLS is disabled; synchronous publishers will send payloads "
            "in plaintext."
        )

    client.enable_logger(logger)
    return client


def publish_with_retries(
    topic: str,
    payload: str,
    config: RuntimeConfig,
    *,
    logger: logging.Logger,
    retries: int = 3,
    publish_timeout: float = 4.0,
    base_delay: float = 0.5,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_interval: float = 0.05,
    tls_material: TLSMaterial | None = None,
    client_module: Any | None = None,
) -> None:
    """Publish *payload* to *topic* with retry semantics."""

    if retries <= 0:
        raise ValueError("retries must be a positive integer")
    if poll_interval <= 0:
        poll_interval = 0.05

    def _log_retry(state: RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome else None
        logger.warning(
            "MQTT publish attempt %d failed: %s",
            state.attempt_number,
            exc,
        )

    def _attempt() -> None:
        client = _build_client(
            config,
            logger=logger,
            tls_material=tls_material,
            client_module=client_module or mqtt_client,
        )

        try:
            client.connect(config.mqtt_host, config.mqtt_port, keepalive=60)
            loop_start = getattr(client, "loop_start", None)
            if callable(loop_start):
                loop_start()

            result = client.publish(topic, payload, qos=1, retain=False)

            if publish_timeout <= 0:
                raise TimeoutError("MQTT publish timed out before completion")

            deadline = time.monotonic() + publish_timeout
            while not result.is_published():
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        (
                            "MQTT publish timed out after "
                            f"{publish_timeout} seconds"
                        )
                    )
                sleep_fn(poll_interval)

            logger.info("Published to %s with payload %s", topic, payload)
        finally:
            for cleanup in ("loop_stop", "disconnect"):
                method = getattr(client, cleanup, None)
                if callable(method):
                    try:
                        method()
                    except Exception:  # pragma: no cover - defensive cleanup
                        logger.debug(
                            "Cleanup %s failed", cleanup, exc_info=True
                        )

    adjusted_base = max(base_delay, 0.0)
    wait_kwargs: dict[str, float] = {
        "multiplier": adjusted_base or 0.0,
        "min": adjusted_base or 0.0,
    }
    if adjusted_base > 0:
        wait_kwargs["max"] = adjusted_base * 8

    wait = wait_exponential(**wait_kwargs)

    retryer = Retrying(
        reraise=True,
        stop=stop_after_attempt(retries),
        wait=wait,
        retry=retry_if_exception_type(Exception),
        before_sleep=_log_retry,
        sleep=sleep_fn,
    )

    retryer(_attempt)


__all__ = [
    "publish_with_retries",
    "build_client",
]
