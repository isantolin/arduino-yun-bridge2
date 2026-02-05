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
import time
from collections.abc import Awaitable, Callable
from typing import NoReturn

import msgspec

import tenacity

# [SIL-2] Deterministic Import: uvloop is MANDATORY for performance on OpenWrt.
# This must fail immediately if python3-uvloop is not installed.
import uvloop

from mcubridge.config.logging import configure_logging
from mcubridge.config.settings import RuntimeConfig, load_runtime_config, get_config_source
from mcubridge.config.const import (
    DEFAULT_SERIAL_SHARED_SECRET,
    SUPERVISOR_DEFAULT_MAX_BACKOFF,
    SUPERVISOR_DEFAULT_MIN_BACKOFF,
    SUPERVISOR_DEFAULT_RESTART_INTERVAL,
    SUPERVISOR_PROMETHEUS_RESTART_INTERVAL,
    SUPERVISOR_STATUS_MAX_BACKOFF,
    SUPERVISOR_STATUS_RESTART_INTERVAL,
)
from mcubridge.metrics import (
    PrometheusExporter,
    publish_bridge_snapshots,
    publish_metrics,
)
from mcubridge.security.security import verify_crypto_integrity
from mcubridge.services.runtime import BridgeService, SerialHandshakeFatal
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.state.status import cleanup_status_file, status_writer
from mcubridge.transport import (
    SerialTransport,
    mqtt_task,
    serial_sender_not_ready,
)
from mcubridge.watchdog import WatchdogKeepalive

logger = logging.getLogger("mcubridge")


class SupervisedTaskSpec(msgspec.Struct):
    """Specification for a supervised async task."""

    name: str
    factory: Callable[[], Awaitable[None]]
    fatal_exceptions: tuple[type[BaseException], ...] = ()
    max_restarts: int | None = None
    restart_interval: float = SUPERVISOR_DEFAULT_RESTART_INTERVAL
    min_backoff: float = SUPERVISOR_DEFAULT_MIN_BACKOFF
    max_backoff: float = SUPERVISOR_DEFAULT_MAX_BACKOFF


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
        if self.config.bridge_summary_interval > 0.0 or self.config.bridge_handshake_interval > 0.0:
            specs.append(
                SupervisedTaskSpec(
                    name="bridge-snapshots",
                    factory=self._run_bridge_snapshots,
                    max_restarts=5,
                    restart_interval=SUPERVISOR_STATUS_RESTART_INTERVAL,
                    max_backoff=SUPERVISOR_STATUS_MAX_BACKOFF,
                )
            )

        if self.config.watchdog_enabled:
            self.watchdog = WatchdogKeepalive(
                interval=self.config.watchdog_interval,
                state=self.state,
            )
            logger.info("Watchdog enabled (interval=%.2fs)", self.config.watchdog_interval)
            specs.append(
                SupervisedTaskSpec(
                    name="watchdog",
                    factory=self.watchdog.run,
                    max_restarts=5,
                    restart_interval=SUPERVISOR_STATUS_RESTART_INTERVAL,
                    max_backoff=SUPERVISOR_STATUS_MAX_BACKOFF,
                )
            )

        if self.config.metrics_enabled:
            self.exporter = PrometheusExporter(
                self.state,
                self.config.metrics_host,
                self.config.metrics_port,
            )
            specs.append(
                SupervisedTaskSpec(
                    name="prometheus-exporter",
                    factory=self.exporter.run,
                    max_restarts=5,
                    restart_interval=SUPERVISOR_PROMETHEUS_RESTART_INTERVAL,
                )
            )

        return specs

    async def _supervise_task(self, spec: SupervisedTaskSpec) -> None:
        """Run *coro_factory* restarting it on failures using tenacity."""
        log = logging.getLogger("mcubridge.supervisor")
        callbacks = self._SupervisorCallbacks(spec.name, log, self.state)

        retryer = tenacity.AsyncRetrying(
            wait=tenacity.wait_exponential(multiplier=spec.min_backoff, max=spec.max_backoff),
            retry=tenacity.retry_if_not_exception_type(
                (asyncio.CancelledError, SystemExit, KeyboardInterrupt, GeneratorExit) + spec.fatal_exceptions
            ),
            stop=tenacity.stop_after_attempt(
                spec.max_restarts + 1
            ) if spec.max_restarts is not None else tenacity.stop_never,
            before_sleep=callbacks.before_sleep,
            after=callbacks.after_retry,
            reraise=True,
        )

        last_start_time = 0.0

        try:
            while True:
                try:
                    async for attempt in retryer:
                        with attempt:
                            last_start_time = time.monotonic()
                            await spec.factory()

                            # If we get here, the task exited cleanly.
                            log.warning("%s task exited cleanly; supervisor exiting", spec.name)
                            self.state.mark_supervisor_healthy(spec.name)
                            return
                except tenacity.RetryError:
                    log.error("%s exceeded max restarts (%s); giving up", spec.name, spec.max_restarts)
                    raise
                except spec.fatal_exceptions as exc:
                    log.critical("%s failed with fatal exception: %s", spec.name, exc)
                    self.state.record_supervisor_failure(spec.name, backoff=0.0, exc=exc, fatal=True)
                    raise
                except BaseException:
                    # Check for healthy runtime to reset backoff
                    if last_start_time > 0 and (time.monotonic() - last_start_time) > max(10.0, spec.restart_interval):
                        log.info("%s was healthy long enough; resetting backoff", spec.name)
                        self.state.mark_supervisor_healthy(spec.name)
                        continue

                    # Reraise to let tenacity handle retry or stop
                    raise

        except asyncio.CancelledError:
            log.debug("%s supervisor cancelled", spec.name)
            raise

    class _SupervisorCallbacks:
        """Helper to avoid nested functions in supervisor."""

        __slots__ = ("name", "log", "state")

        def __init__(self, name: str, log: logging.Logger, state: RuntimeState | None):
            self.name = name
            self.log = log
            self.state = state

        def before_sleep(self, retry_state: tenacity.RetryCallState) -> None:
            exc = retry_state.outcome.exception() if retry_state.outcome else None
            delay = retry_state.next_action.sleep if retry_state.next_action else 0.0
            self.log.error("%s failed (%s); restarting in %.1fs", self.name, exc, delay)

        def after_retry(self, retry_state: tenacity.RetryCallState) -> None:
            exc = retry_state.outcome.exception() if retry_state.outcome else None
            if self.state is not None and exc:
                is_last = retry_state.next_action is None
                delay = retry_state.next_action.sleep if retry_state.next_action else 0.0
                self.state.record_supervisor_failure(self.name, backoff=delay, exc=exc, fatal=is_last)

    async def run(self) -> None:
        """Main async entry point."""
        supervised_tasks = self._setup_supervision()

        try:
            async with self.service:
                async with asyncio.TaskGroup() as task_group:
                    for spec in supervised_tasks:
                        task_group.create_task(self._supervise_task(spec))
        except* asyncio.CancelledError:
            logger.info("Main task cancelled; shutting down.")
        except* Exception as exc_group:
            for group_exc in exc_group.exceptions:
                logger.critical(
                    "Unhandled exception in main task group: %s",
                    group_exc,
                    exc_info=group_exc,
                )
            raise
        finally:
            cleanup_status_file()
            logger.info("MCU Bridge daemon stopped.")


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
            logger.critical("Fatal error in task group: %s", group_exc, exc_info=group_exc)
        sys.exit(1)
    except OSError as exc:
        logger.critical("System/OS error during daemon execution: %s", exc, exc_info=True)
        sys.exit(1)
    except BaseException as exc:
        logger.critical(
            "CRITICAL: Unhandled non-standard exception. Terminating: %s",
            exc,
            exc_info=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
