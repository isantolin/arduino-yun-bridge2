#!/usr/bin/env python3
"""Async orchestrator for the Arduino MCU Bridge v2 daemon.

This module contains the main entry point and orchestration logic for the
MCU Bridge daemon, which manages communication between OpenWrt Linux and
the Arduino MCU over serial and MQTT.

[SIL-2 COMPLIANCE]
The daemon implements robust error handling:
- Task supervision with automatic restart and backoff
- Fatal exception handling for unrecoverable serial errors
- Graceful shutdown on SIGTERM/SIGINT
- Status file cleanup on exit

Architecture:
    main() -> BridgeDaemon -> TaskGroup
        ├── serial-link (SerialTransport)
        ├── mqtt-link (mqtt_task)
        ├── status-writer (status_writer)
        ├── metrics-publisher (publish_metrics)
        ├── bridge-snapshots (optional)
        ├── watchdog (optional)
        └── prometheus-exporter (optional)

Usage:
    $ python -m mcubridge.daemon
    # Or via init script:
    $ /etc/init.d/mcubridge start
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING, NoReturn

from mcubridge.config.logging import configure_logging
from mcubridge.config.settings import RuntimeConfig, load_runtime_config
from mcubridge.const import DEFAULT_SERIAL_SHARED_SECRET
from mcubridge.metrics import publish_bridge_snapshots, publish_metrics
from mcubridge.services.runtime import (
    BridgeService,
    SerialHandshakeFatal,
)
from mcubridge.const import (
    SUPERVISOR_PROMETHEUS_RESTART_INTERVAL,
    SUPERVISOR_STATUS_MAX_BACKOFF,
    SUPERVISOR_STATUS_RESTART_INTERVAL,
)
from mcubridge.services.task_supervisor import SupervisedTaskSpec, supervise_task
from mcubridge.state.context import create_runtime_state
from mcubridge.state.status import cleanup_status_file, status_writer
from mcubridge.transport import (
    SerialTransport,
    mqtt_task,
    serial_sender_not_ready,
)
from mcubridge.watchdog import WatchdogKeepalive


logger = logging.getLogger("mcubridge")


if TYPE_CHECKING:
    from mcubridge.metrics import PrometheusExporter


class BridgeDaemon:
    """Main orchestrator for the MCU Bridge daemon services.

    This class manages the lifecycle of all daemon components including
    serial communication, MQTT publishing, metrics, and optional features
    like watchdog and Prometheus exporter.

    Attributes:
        config: Runtime configuration loaded from UCI.
        state: Shared runtime state for all components.
        service: BridgeService instance handling RPC dispatch.
        watchdog: Optional watchdog keepalive task.
        exporter: Optional Prometheus metrics exporter.
    """

    def __init__(self, config: RuntimeConfig):
        """Initialize the daemon with configuration.

        Args:
            config: Validated RuntimeConfig from UCI/defaults.
        """
        self.config = config
        self.state = create_runtime_state(config)
        self.service = BridgeService(config, self.state)
        # Initialize dependencies
        self.service.register_serial_sender(serial_sender_not_ready)
        self.watchdog: WatchdogKeepalive | None = None
        self.exporter: PrometheusExporter | None = None

        if self.config.serial_shared_secret:
            logger.info("Security check passed: Shared secret is configured.")

    async def _run_serial_link(self) -> None:
        transport = SerialTransport(self.config, self.state, self.service)
        await transport.run()

    async def _run_mqtt_link(self) -> None:
        await mqtt_task(self.config, self.state, self.service)

    async def _run_status_writer(self) -> None:
        await status_writer(self.state, self.config.status_interval)

    async def _run_metrics_publisher(self) -> None:
        await publish_metrics(
            self.state,
            self.service.enqueue_mqtt,
            float(self.config.status_interval),
        )

    async def _run_bridge_snapshots(self) -> None:
        await publish_bridge_snapshots(
            self.state,
            self.service.enqueue_mqtt,
            summary_interval=float(self.config.bridge_summary_interval),
            handshake_interval=float(self.config.bridge_handshake_interval),
        )

    def _setup_supervision(self) -> list[SupervisedTaskSpec]:
        """Prepare the list of tasks to be supervised."""
        # Build Spec List
        specs: list[SupervisedTaskSpec] = [
            SupervisedTaskSpec(
                name="serial-link",
                factory=self._run_serial_link,
                fatal_exceptions=(SerialHandshakeFatal,),
            ),
            SupervisedTaskSpec(
                name="mqtt-link",
                factory=self._run_mqtt_link,
            ),
            SupervisedTaskSpec(
                name="status-writer",
                factory=self._run_status_writer,
                max_restarts=5,
                restart_interval=SUPERVISOR_STATUS_RESTART_INTERVAL,
                max_backoff=SUPERVISOR_STATUS_MAX_BACKOFF,
            ),
            SupervisedTaskSpec(
                name="metrics-publisher",
                factory=self._run_metrics_publisher,
                max_restarts=5,
                restart_interval=SUPERVISOR_STATUS_RESTART_INTERVAL,
                max_backoff=SUPERVISOR_STATUS_MAX_BACKOFF,
            ),
        ]

        # 3. Optional Features
        if self.config.bridge_summary_interval > 0.0 or \
           self.config.bridge_handshake_interval > 0.0:
            specs.append(SupervisedTaskSpec(
                name="bridge-snapshots",
                factory=self._run_bridge_snapshots,
                max_restarts=5,
                restart_interval=SUPERVISOR_STATUS_RESTART_INTERVAL,
                max_backoff=SUPERVISOR_STATUS_MAX_BACKOFF,
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
                restart_interval=SUPERVISOR_STATUS_RESTART_INTERVAL,
                max_backoff=SUPERVISOR_STATUS_MAX_BACKOFF,
            ))

        if self.config.metrics_enabled:
            from mcubridge.metrics import PrometheusExporter

            self.exporter = PrometheusExporter(
                self.state,
                self.config.metrics_host,
                self.config.metrics_port,
            )
            specs.append(SupervisedTaskSpec(
                name="prometheus-exporter",
                factory=self.exporter.run,
                max_restarts=5,
                restart_interval=SUPERVISOR_PROMETHEUS_RESTART_INTERVAL,
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
            for group_exc in exc_group.exceptions:
                logger.critical(
                    "Unhandled exception in main task group",
                    exc_info=group_exc,
                )
            raise
        finally:
            cleanup_status_file()
            logger.info("MCU Bridge daemon stopped.")


def main() -> NoReturn:  # pragma: no cover (Entry point wrapper)
    config = load_runtime_config()
    configure_logging(config)

    logger.info(
        "Starting MCU Bridge daemon. Serial: %s@%d MQTT: %s:%d",
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
            " Please run 'mcubridge-rotate-credentials' IMMEDIATELY.\n"
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
        for group_exc in exc_group.exceptions:
            logger.critical("Fatal error in main execution", exc_info=group_exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
