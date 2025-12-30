#!/usr/bin/env python3
"""Async orchestrator for the Arduino Yun Bridge v2 daemon."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import NoReturn

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
    SerialTransport,
    mqtt_task,
    serial_sender_not_ready,
)
from yunbridge.watchdog import WatchdogKeepalive


logger = logging.getLogger("yunbridge")


class BridgeDaemon:
    """Orchestrator for the Yun Bridge services."""

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.state = create_runtime_state(config)
        self.service = BridgeService(config, self.state)
        # Initialize dependencies
        self.service.register_serial_sender(serial_sender_not_ready)
        self.watchdog: WatchdogKeepalive | None = None
        self.exporter: PrometheusExporter | None = None

        if self.config.serial_shared_secret:
            logger.info("Security check passed: Shared secret is configured.")

    def _setup_supervision(self) -> list[SupervisedTaskSpec]:
        """Prepare the list of tasks to be supervised."""
        
        # 1. Define Core Tasks (Inner functions to capture 'self')
        async def _serial_runner() -> None:
            transport = SerialTransport(self.config, self.state, self.service)
            await transport.run()

        async def _mqtt_runner() -> None:
            await mqtt_task(self.config, self.state, self.service)

        async def _status_runner() -> None:
            await status_writer(self.state, self.config.status_interval)

        async def _metrics_runner() -> None:
            await publish_metrics(
                self.state,
                self.service.enqueue_mqtt,
                float(self.config.status_interval),
            )

        async def _bridge_snapshots_runner() -> None:
            await publish_bridge_snapshots(
                self.state,
                self.service.enqueue_mqtt,
                summary_interval=float(self.config.bridge_summary_interval),
                handshake_interval=float(self.config.bridge_handshake_interval),
            )

        # 2. Build Spec List
        specs: list[SupervisedTaskSpec] = [
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

        # 3. Optional Features
        if self.config.bridge_summary_interval > 0.0 or \
           self.config.bridge_handshake_interval > 0.0:
            specs.append(SupervisedTaskSpec(
                name="bridge-snapshots",
                factory=_bridge_snapshots_runner,
                max_restarts=5,
                restart_interval=120.0,
                max_backoff=10.0,
            ))

        if self.config.watchdog_enabled:
            self.watchdog = WatchdogKeepalive(
                interval=self.config.watchdog_interval,
                state=self.state,
            )
            logger.info("Watchdog enabled (interval=%.2fs)", self.config.watchdog_interval)
            specs.append(SupervisedTaskSpec(
                name="watchdog",
                factory=self.watchdog.run,
                max_restarts=5,
                restart_interval=120.0,
                max_backoff=10.0,
            ))

        if self.config.metrics_enabled:
            self.exporter = PrometheusExporter(
                self.state,
                self.config.metrics_host,
                self.config.metrics_port,
            )
            specs.append(SupervisedTaskSpec(
                name="prometheus-exporter",
                factory=self.exporter.run,
                max_restarts=5,
                restart_interval=300.0,
            ))

        return specs

    async def run(self) -> None:
        """Main async entry point."""
        supervised_tasks = self._setup_supervision()
        
        try:
            async with self.service:
                async with asyncio.TaskGroup() as task_group:
                    for spec in supervised_tasks:
                        task_group.create_task(
                            supervise_task(
                                spec.name,
                                spec.factory,
                                fatal_exceptions=spec.fatal_exceptions,
                                min_backoff=spec.min_backoff,
                                max_backoff=spec.max_backoff,
                                state=self.state,
                                max_restarts=spec.max_restarts,
                                restart_interval=spec.restart_interval,
                            )
                        )
        except* asyncio.CancelledError:
            logger.info("Main task cancelled; shutting down.")
        except* Exception as exc_group:
            for exc in exc_group.exceptions:
                logger.critical("Unhandled exception in main task group", exc_info=exc)
            raise
        finally:
            cleanup_status_file()
            logger.info("Yun Bridge daemon stopped.")


def main() -> NoReturn:  # pragma: no cover (Entry point wrapper)
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
        daemon = BridgeDaemon(config)
        asyncio.run(daemon.run())
        sys.exit(0)
    except KeyboardInterrupt:
        logger.info("Daemon interrupted by user.")
        sys.exit(0)
    except RuntimeError as exc:
        logger.critical("Startup aborted: %s", exc)
        sys.exit(1)
    except ExceptionGroup as exc_group:
        for exc in exc_group.exceptions:
            logger.critical("Fatal error in main execution", exc_info=exc)
        sys.exit(1)
    except Exception:
        logger.critical("Fatal error in main execution", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
