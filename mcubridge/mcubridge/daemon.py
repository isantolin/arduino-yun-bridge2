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
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any, Annotated

import msgspec
import tenacity
import typer

# [SIL-2] Deterministic Import: uvloop is MANDATORY for performance on OpenWrt.
import structlog
import uvloop

from mcubridge.config.const import (
    DEFAULT_SERIAL_SHARED_SECRET,
    SUPERVISOR_DEFAULT_MAX_BACKOFF,
    SUPERVISOR_DEFAULT_MIN_BACKOFF,
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
from mcubridge.state.status import STATUS_FILE, status_writer
from mcubridge.transport import (
    MqttTransport,
    SerialTransport,
)
from mcubridge.watchdog import WatchdogKeepalive

logger = structlog.get_logger("mcubridge")


def _cleanup_child_processes() -> None:
    """Terminates all child processes spawned by this daemon using direct psutil delegation."""
    import psutil
    import contextlib

    try:
        parent = psutil.Process(os.getpid())
        children = parent.children(recursive=True)

        # 1. Terminate all
        for p in children:
            with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
                p.terminate()

        # 2. Wait for termination
        _, alive = psutil.wait_procs(children, timeout=3.0)

        # 3. Force kill survivors
        for p in alive:
            with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
                logger.warning("Force killing zombie process %d", p.pid)
                p.kill()

    except (psutil.NoSuchProcess, ProcessLookupError):
        pass
    except psutil.Error as e:
        logger.error("Error during process tree cleanup: %s", e)


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
        self.mqtt_transport = MqttTransport(self.config, self.state)
        self.mqtt_transport.configure_spool(
            self.config.mqtt_spool_dir, self.config.mqtt_queue_limit * 4
        )
        self.mqtt_transport.initialize_spool()
        self.service = BridgeService(config, self.state, self.mqtt_transport)
        self.mqtt_transport.set_service(self.service)
        # Initialize dependencies

        self.watchdog: WatchdogKeepalive | None = None
        self.exporter: PrometheusExporter | None = None

        if self.config.serial_shared_secret:
            logger.info("Security check passed: Shared secret is configured.")

    async def run(self) -> None:
        """Main entry point for daemon execution using native TaskGroup orchestration."""
        log = structlog.get_logger("mcubridge.daemon")

        try:
            async with self.service:
                async with asyncio.TaskGroup() as tg:
                    # 1. Serial Link (Critical)
                    tg.create_task(
                        self._supervise(
                            "serial-link",
                            lambda: SerialTransport(
                                self.config, self.state, self.service
                            ).run(),
                            (SerialHandshakeFatal,),
                        )
                    )

                    # 2. MQTT Link
                    tg.create_task(
                        self._supervise(
                            "mqtt-link",
                            self.mqtt_transport.run,
                        )
                    )

                    # 3. Status & Metrics (Periodic)
                    tg.create_task(
                        self._supervise(
                            "status-writer",
                            lambda: status_writer(
                                self.state, self.config.status_interval
                            ),
                        )
                    )
                    tg.create_task(
                        self._supervise(
                            "metrics-publisher",
                            lambda: publish_metrics(
                                self.state,
                                self.mqtt_transport.enqueue_mqtt,
                                float(self.config.status_interval),
                            ),
                        )
                    )

                    # 4. Optional Features
                    if (
                        self.config.bridge_summary_interval > 0.0
                        or self.config.bridge_handshake_interval > 0.0
                    ):
                        tg.create_task(
                            self._supervise(
                                "bridge-snapshots",
                                lambda: publish_bridge_snapshots(
                                    self.state,
                                    self.mqtt_transport.enqueue_mqtt,
                                    summary_interval=float(
                                        self.config.bridge_summary_interval
                                    ),
                                    handshake_interval=float(
                                        self.config.bridge_handshake_interval
                                    ),
                                ),
                            )
                        )

                    if self.config.watchdog_enabled:
                        self.watchdog = WatchdogKeepalive(
                            interval=self.config.watchdog_interval, state=self.state
                        )
                        tg.create_task(self._supervise("watchdog", self.watchdog.run))

                    if self.config.metrics_enabled:
                        self.exporter = PrometheusExporter(
                            self.state,
                            self.config.metrics_host,
                            self.config.metrics_port,
                        )
                        tg.create_task(
                            self._supervise("prometheus-exporter", self.exporter.run)
                        )

        except* asyncio.CancelledError:
            log.info("Daemon shutdown initiated (Cancelled).")
        except* Exception as exc_group:
            for exc in exc_group.exceptions:
                log.critical("Fatal task error: %s", exc, exc_info=exc)
            raise
        finally:
            self.state.cleanup()
            _cleanup_child_processes()
            STATUS_FILE.unlink(missing_ok=True)
            log.info("MCU Bridge daemon stopped.")

    async def _supervise(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
        fatal_exceptions: tuple[type[BaseException], ...] = (),
        max_restarts: int | None = None,
        min_backoff: float = SUPERVISOR_DEFAULT_MIN_BACKOFF,
        max_backoff: float = SUPERVISOR_DEFAULT_MAX_BACKOFF,
    ) -> None:
        """Lightweight supervisor with Circuit Breaker logic (SIL 2)."""
        # [SIL-2] Circuit Breaker: Stop after 10 consecutive failures at max backoff
        # to prevent infinite CPU thrashing on persistent hardware failure.
        max_consecutive_max_backoff = 10

        def _circuit_breaker(rs: tenacity.RetryCallState) -> bool:
            if not rs.outcome or not rs.outcome.failed:
                return False
            exc = rs.outcome.exception()
            if isinstance(exc, (*fatal_exceptions, asyncio.CancelledError)):
                return False
            # Check for consecutive max backoff hits
            if (
                rs.idle_for >= max_backoff
                and rs.attempt_number >= max_consecutive_max_backoff
            ):
                logger.critical(
                    "CIRCUIT BREAKER: Task '%s' tripped after repeated failures at max backoff.",
                    name,
                )
                return False
            return True

        retryer = tenacity.AsyncRetrying(
            stop=(
                tenacity.stop_after_attempt(max_restarts + 1)
                if max_restarts is not None
                else tenacity.stop_never
            ),
            wait=tenacity.wait_exponential(multiplier=min_backoff, max=max_backoff),
            retry=_circuit_breaker,
            before_sleep=lambda rs: self.state.record_supervisor_failure(
                name,
                backoff=float(rs.next_action.sleep if rs.next_action else 0.0),
                exc=rs.outcome.exception() if rs.outcome else None,
            ),
            reraise=True,
        )

        async for attempt in retryer:
            with attempt:
                await factory()


def main(argv: list[str] | None = None) -> None:
    """Main entry point for the MCU Bridge daemon."""
    parser = argparse.ArgumentParser(
        description="Arduino MCU Bridge Daemon v2",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--serial-port", help="Serial port to use")
    parser.add_argument("--serial-baud", type=int, help="Serial baud rate")
    parser.add_argument("--mqtt-host", help="MQTT host")
    parser.add_argument("--mqtt-port", type=int, help="MQTT port")
    parser.add_argument("--mqtt-tls", type=int, help="Use TLS for MQTT (0 or 1)")
    parser.add_argument("--serial-shared-secret", help="Shared secret for serial link")
    parser.add_argument(
        "--allowed-commands", help="Comma-separated list of allowed shell commands"
    )
    parser.add_argument(
        "--non-interactive", action="store_true", help="Enable non-interactive mode"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args(argv)

    overrides: dict[str, Any] = {}
    if args.serial_port:
        overrides["serial_port"] = args.serial_port
    if args.serial_baud:
        overrides["serial_baud"] = args.serial_baud
    if args.mqtt_host:
        overrides["mqtt_host"] = args.mqtt_host
    if args.mqtt_port:
        overrides["mqtt_port"] = args.mqtt_port
    if args.mqtt_tls is not None:
        overrides["mqtt_tls"] = bool(args.mqtt_tls)
    if args.serial_shared_secret:
        overrides["serial_shared_secret"] = args.serial_shared_secret
    if args.non_interactive:
        overrides["non_interactive"] = True
    if args.debug:
        overrides["debug"] = True
    if args.allowed_commands:
        overrides["allowed_commands"] = (
            args.allowed_commands.split(",") if args.allowed_commands != "*" else "*"
        )

    config = load_runtime_config(overrides)
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
            " [STRICT PROVISIONING] Network services (MQTT) are BLOCKED.\n"
            " Please run 'mcubridge-rotate-credentials' IMMEDIATELY.\n"
            "****************************************************************"
        )
        # In strict mode, we force the MQTT config to disabled if secret is default
        config = msgspec.structs.replace(config, mqtt_enabled=False)
        logger.warning("STRICT MODE: MQTT transport has been DISABLED for security.")

    daemon = None
    try:
        if uvloop is None:
            raise RuntimeError("python3-uvloop is required")
        daemon = BridgeDaemon(config)

        # [SIL-2] Unified entry point via asyncio.Runner (Python 3.11+)
        # This handles signal registration and loop lifecycle deterministically.
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            runner.run(daemon.run())

    except KeyboardInterrupt:
        logger.info("Daemon interrupted by user.")
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        asyncio.TimeoutError,
        msgspec.MsgspecError,
        tenacity.RetryError,
    ) as exc:
        logger.critical(
            "Fatal error: %s", exc, exc_info=not isinstance(exc, RuntimeError)
        )
        sys.exit(1)
    except BaseException as exc:
        # [SIL-2] Catch-all for unhandled system-level errors
        logger.critical("Unhandled system error: %s", exc, exc_info=True)
        sys.exit(1)
    finally:
        if daemon is not None:
            daemon.state.cleanup()


if __name__ == "__main__":
    app()
