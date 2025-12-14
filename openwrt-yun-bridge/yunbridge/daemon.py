#!/usr/bin/env python3
"""Async orchestrator for the Arduino Yun Bridge v2 daemon."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import cast

from yunbridge.config.logging import configure_logging
from yunbridge.config.settings import RuntimeConfig, load_runtime_config
from yunbridge.const import DEFAULT_SERIAL_SHARED_SECRET
from yunbridge.metrics import (
    PrometheusExporter,
    publish_bridge_snapshots,
    publish_metrics,
)
from yunbridge.services.runtime import (
    BridgeService,
    SerialHandshakeFatal,
)
from yunbridge.services.task_supervisor import SupervisedTaskSpec, supervise_task
from yunbridge.state.context import create_runtime_state
from yunbridge.state.status import cleanup_status_file, status_writer
from yunbridge.transport import (
    build_mqtt_tls_context,
    mqtt_task,
    serial_reader_task,
    serial_sender_not_ready,
)
from yunbridge.watchdog import WatchdogKeepalive


logger = logging.getLogger("yunbridge")


async def main_async(config: RuntimeConfig) -> None:
    if config.serial_shared_secret:
        logger.info("Security check passed: Shared secret is configured and not default.")

    state = create_runtime_state(config)
    service = BridgeService(config, state)
    service.register_serial_sender(serial_sender_not_ready)

    try:
        tls_context = build_mqtt_tls_context(config)
    except Exception as exc:
        raise RuntimeError(f"TLS configuration invalid: {exc}") from exc

    async def _serial_runner() -> None:
        await serial_reader_task(config, state, service)

    async def _mqtt_runner() -> None:
        await mqtt_task(config, state, service, tls_context)

    async def _status_runner() -> None:
        await status_writer(state, config.status_interval)

    async def _metrics_runner() -> None:
        await publish_metrics(
            state,
            service.enqueue_mqtt,
            float(config.status_interval),
        )

    async def _bridge_snapshots_runner() -> None:
        await publish_bridge_snapshots(
            state,
            service.enqueue_mqtt,
            summary_interval=float(config.bridge_summary_interval),
            handshake_interval=float(config.bridge_handshake_interval),
        )

    supervised_tasks: list[SupervisedTaskSpec] = [
        SupervisedTaskSpec(
            name="serial-link",
            factory=_serial_runner,
            fatal_exceptions=(SerialHandshakeFatal,),
        ),
        SupervisedTaskSpec(
            name="mqtt-link",
            factory=_mqtt_runner,
        ),
        SupervisedTaskSpec(
            name="status-writer",
            factory=_status_runner,
            max_restarts=5,
            restart_interval=120.0,
            max_backoff=10.0,
        ),
        SupervisedTaskSpec(
            name="metrics-publisher",
            factory=_metrics_runner,
            max_restarts=5,
            restart_interval=120.0,
            max_backoff=10.0,
        ),
    ]

    if config.bridge_summary_interval > 0.0 or config.bridge_handshake_interval > 0.0:
        supervised_tasks.append(
            SupervisedTaskSpec(
                name="bridge-snapshots",
                factory=_bridge_snapshots_runner,
                max_restarts=5,
                restart_interval=120.0,
                max_backoff=10.0,
            )
        )

    if config.watchdog_enabled:
        watchdog = WatchdogKeepalive(
            interval=config.watchdog_interval,
            state=state,
        )
        logger.info(
            "Starting watchdog keepalive at %.2f second interval",
            config.watchdog_interval,
        )
        supervised_tasks.append(
            SupervisedTaskSpec(
                name="watchdog",
                factory=watchdog.run,
                max_restarts=5,
                restart_interval=120.0,
                max_backoff=10.0,
            )
        )

    exporter: PrometheusExporter | None = None
    if config.metrics_enabled:
        exporter = PrometheusExporter(
            state,
            config.metrics_host,
            config.metrics_port,
        )
        supervised_tasks.append(
            SupervisedTaskSpec(
                name="prometheus-exporter",
                factory=exporter.run,
                max_restarts=5,
                restart_interval=300.0,
            )
        )

    try:
        async with service:
            async with asyncio.TaskGroup() as task_group:
                for spec in supervised_tasks:
                    task_group.create_task(
                        supervise_task(
                            spec.name,
                            spec.factory,
                            fatal_exceptions=spec.fatal_exceptions,
                            min_backoff=spec.min_backoff,
                            max_backoff=spec.max_backoff,
                            state=state,
                            max_restarts=spec.max_restarts,
                            restart_interval=spec.restart_interval,
                        )
                    )
    except* asyncio.CancelledError:
        logger.info("Main task cancelled; shutting down.")
    except* Exception as exc_group:
        group_exc = cast(BaseExceptionGroup[BaseException], exc_group)
        for exc in getattr(group_exc, "exceptions", ()):  # pragma: no branch
            logger.critical(
                "Unhandled exception in main task group",
                exc_info=exc,
            )
        raise
    finally:
        cleanup_status_file()
        logger.info("Yun Bridge daemon stopped.")


def main() -> None:
    config = load_runtime_config()
    configure_logging(config)

    logger.info(
        "Starting Yun Bridge daemon. Serial: %s@%d MQTT: %s:%d",
        config.serial_port,
        config.serial_baud,
        config.mqtt_host,
        config.mqtt_port,
    )

    if config.serial_shared_secret == DEFAULT_SERIAL_SHARED_SECRET:
        logger.critical(
            "****************************************************************\n"
            " SECURITY CRITICAL: Using default serial shared secret!\n"
            " This device is VULNERABLE to local attacks.\n"
            " Please run 'yunbridge-rotate-credentials' IMMEDIATELY.\n"
            "****************************************************************"
        )

    try:
        asyncio.run(main_async(config))
    except KeyboardInterrupt:
        logger.info("Daemon interrupted by user.")
    except RuntimeError as exc:
        logger.critical("Startup aborted: %s", exc)
        sys.exit(1)
    except ExceptionGroup as exc_group:
        typed_exc_group = cast(BaseExceptionGroup[BaseException], exc_group)
        for exc in typed_exc_group.exceptions:
            logger.critical("Fatal error in main execution", exc_info=exc)
    except Exception:
        logger.critical("Fatal error in main execution", exc_info=True)


if __name__ == "__main__":
    main()
