#!/usr/bin/env python3
"""Async orchestrator for the Arduino MCU Bridge v2 daemon.

This module contains the main entry point and orchestration logic for the
MCU Bridge daemon, which manages communication between OpenWrt Linux and
the Arduino MCU over serial and MQTT.

[SIL-2 COMPLIANCE]
The daemon implements robust error handling:
- Deterministic startup (Fail-Fast on missing deps)
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
        ├── prometheus-exporter (optional)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import NoReturn

import msgspec
import psutil
import tenacity

# [SIL-2] Deterministic Import: uvloop is MANDATORY for performance on OpenWrt.
try:
    import uvloop
except ModuleNotFoundError:  # pragma: no cover
    uvloop = None

from mcubridge.config.const import (
    DEFAULT_SERIAL_SHARED_SECRET,
    SUPERVISOR_DEFAULT_MAX_BACKOFF,
    SUPERVISOR_DEFAULT_MIN_BACKOFF,
    SUPERVISOR_DEFAULT_RESTART_INTERVAL,
)
from mcubridge.config.logging import configure_logging
from mcubridge.config.settings import (
    RuntimeConfig,
    get_config_source,
    load_runtime_config,
)
from mcubridge.metrics import (
    PrometheusExporter,
    publish_bridge_snapshots,
    publish_metrics,
)
from mcubridge.security.security import verify_crypto_integrity
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.state.status import cleanup_status_file, status_writer
from mcubridge.transport import (
    MqttTransport,
    SerialTransport,
    serial_sender_not_ready,
)
from mcubridge.watchdog import WatchdogKeepalive

logger = logging.getLogger("mcubridge")
SUPERVISOR_RECOVERABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    ConnectionError,
    OSError,
    RuntimeError,
    TimeoutError,
    ValueError,
    msgspec.MsgspecError,
)


class SupervisedTaskSpec(msgspec.Struct):
    """Specification for a supervised async task (Legacy compatibility)."""

    name: str
    factory: Callable[[], Awaitable[None]]
    fatal_exceptions: tuple[type[BaseException], ...] = ()
    max_restarts: int | None = None
    restart_interval: float = SUPERVISOR_DEFAULT_RESTART_INTERVAL
    min_backoff: float = SUPERVISOR_DEFAULT_MIN_BACKOFF
    max_backoff: float = SUPERVISOR_DEFAULT_MAX_BACKOFF


def _cleanup_child_processes() -> None:
    """Terminates all child processes spawned by this daemon."""
    try:
        current_process = psutil.Process()
        children = current_process.children(recursive=True)
        if not children:
            return

        logger.info("Cleaning up %d child processes...", len(children))
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass

        _, alive = psutil.wait_procs(children, timeout=3.0)
        for child in alive:
            try:
                logger.warning("Force killing zombie process %d", child.pid)
                child.kill()
            except psutil.NoSuchProcess:
                pass
    except psutil.Error as e:
        logger.error("Error during process cleanup: %s", e)


async def _dummy_task() -> None:
    pass

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
        self.state.config_source = get_config_source()
        self.service = BridgeService(config, self.state)
        # Initialize dependencies
        self.service.register_serial_sender(serial_sender_not_ready)
        self.watchdog: WatchdogKeepalive | None = None
        self.exporter: PrometheusExporter | None = None

        if self.config.serial_shared_secret:
            logger.info("Security check passed: Shared secret is configured.")

    async def run(self) -> None:
        """Main entry point for daemon execution using native TaskGroup orchestration."""
        log = logging.getLogger("mcubridge.daemon")

        try:
            async with self.service:
                async with asyncio.TaskGroup() as tg:
                    # 1. Serial Link (Critical)
                    tg.create_task(self._supervise("serial-link", self._run_serial_link, (SerialHandshakeFatal,)))

                    # 2. MQTT Link
                    tg.create_task(self._supervise("mqtt-link", self._run_mqtt_link))

                    # 3. Status & Metrics (Periodic)
                    tg.create_task(self._supervise("status-writer", self._run_status_writer))
                    tg.create_task(self._supervise("metrics-publisher", self._run_metrics_publisher))

                    # 4. Optional Features
                    if self.config.bridge_summary_interval > 0.0 or self.config.bridge_handshake_interval > 0.0:
                        tg.create_task(self._supervise("bridge-snapshots", self._run_bridge_snapshots))

                    if self.config.watchdog_enabled:
                        self.watchdog = WatchdogKeepalive(interval=self.config.watchdog_interval, state=self.state)
                        tg.create_task(self._supervise("watchdog", self.watchdog.run))

                    if self.config.metrics_enabled:
                        self.exporter = PrometheusExporter(
                            self.state,
                            self.config.metrics_host,
                            self.config.metrics_port
                        )
                        tg.create_task(self._supervise("prometheus-exporter", self.exporter.run))

        except* asyncio.CancelledError:
            log.info("Daemon shutdown initiated (Cancelled).")
        except* Exception as exc_group:
            for exc in exc_group.exceptions:
                log.critical("Fatal task error: %s", exc, exc_info=exc)
            raise
        finally:
            _cleanup_child_processes()
            cleanup_status_file()
            log.info("MCU Bridge daemon stopped.")

    async def _run_serial_link(self) -> None:
        transport = SerialTransport(self.config, self.state, self.service)
        await transport.run()

    async def _run_mqtt_link(self) -> None:
        transport = MqttTransport(self.config, self.state, self.service)
        await transport.run()

    async def _run_status_writer(self) -> None:
        await status_writer(self.state, self.config.status_interval)

    async def _run_metrics_publisher(self) -> None:
        await publish_metrics(self.state, self.service.enqueue_mqtt, float(self.config.status_interval))

    async def _run_bridge_snapshots(self) -> None:
        await publish_bridge_snapshots(
            self.state,
            self.service.enqueue_mqtt,
            summary_interval=float(self.config.bridge_summary_interval),
            handshake_interval=float(self.config.bridge_handshake_interval)
        )

    async def _supervise(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
        fatal_exceptions: tuple[type[BaseException], ...] = (),
        max_restarts: int | None = None,
        min_backoff: float = SUPERVISOR_DEFAULT_MIN_BACKOFF,
        max_backoff: float = SUPERVISOR_DEFAULT_MAX_BACKOFF,
    ) -> None:
        """Lightweight supervisor for individual tasks using tenacity."""
        stop = tenacity.stop_after_attempt(max_restarts + 1) if max_restarts is not None else tenacity.stop_never

        retryer = tenacity.AsyncRetrying(
            stop=stop,
            wait=tenacity.wait_exponential(multiplier=min_backoff, max=max_backoff),
            retry=tenacity.retry_if_exception_type(Exception) & tenacity.retry_if_not_exception_type(fatal_exceptions),
            reraise=True
        )

        async for attempt in retryer:
            with attempt:
                try:
                    await factory()
                except Exception as exc:
                    self.state.record_supervisor_failure(name, backoff=0.0, exc=exc)
                    raise

def main() -> NoReturn:  # pragma: no cover (Entry point wrapper)
    config = load_runtime_config()
    configure_logging(config)

    # [MIL-SPEC] FIPS 140-3 Power-On Self-Tests (POST)
    if not verify_crypto_integrity():
        logger.critical("CRYPTOGRAPHIC INTEGRITY CHECK FAILED! Aborting for security.")
        sys.exit(1)

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
        if uvloop is None:
            raise RuntimeError("python3-uvloop is required but not installed")
        daemon = BridgeDaemon(config)
        # [SIL-2] Enforce uvloop for deterministic async performance
        asyncio.run(daemon.run(), loop_factory=uvloop.new_event_loop)
        sys.exit(0)
    except KeyboardInterrupt:
        logger.info("Daemon interrupted by user.")
        sys.exit(0)
    except RuntimeError as exc:
        logger.critical("Startup aborted due to runtime error: %s", exc)
        sys.exit(1)
    except ExceptionGroup as exc_group:
        for group_exc in exc_group.exceptions:
            logger.critical(
                "Fatal error in task group: %s", group_exc, exc_info=group_exc
            )
        sys.exit(1)
    except OSError as exc:
        logger.critical(
            "System/OS error during daemon execution: %s", exc, exc_info=True
        )
        sys.exit(1)
    except BaseException as exc:
        logger.critical(
            "Fatal base exception during daemon execution: %s", exc, exc_info=True
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
