"""Main entry point for the McuBridge daemon."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable

import msgspec
import tenacity

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
from mcubridge.state.status import cleanup_status_file, status_writer
from mcubridge.transport import (
    MqttTransport,
    SerialTransport,
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


class McuBridgeDaemon:
    """Orchestrates the MCU Bridge services and background tasks."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.state = create_runtime_state(config)
        self.state.config_source = get_config_source()
        self.service = BridgeService(self.state, self.config)
        self.watchdog: WatchdogKeepalive | None = None
        self.exporter: PrometheusExporter | None = None

    async def run(self) -> None:
        """Main daemon loop."""
        configure_logging(self.config)
        logger.info("Starting MCU Bridge daemon...")

        if self.config.serial_shared_secret == DEFAULT_SERIAL_SHARED_SECRET:
            logger.warning("Using default serial shared secret! Please update configuration.")

        if not verify_crypto_integrity():
            logger.critical("Cryptographic integrity check failed! FIPS 140-3 compliance violation.")
            sys.exit(1)

        try:
            async with asyncio.TaskGroup() as tg:
                # 1. Transport Layers
                tg.create_task(
                    self._supervise(
                        "serial-transport",
                        lambda: SerialTransport(self.config, self.state, self.service).run(),
                    )
                )
                tg.create_task(
                    self._supervise(
                        "mqtt-link",
                        lambda: MqttTransport(self.state, self.config, self.service).run(),
                    )
                )

                # 2. Core Service
                tg.create_task(self._supervise("bridge-service", self.service.run))

                # 3. Observability
                tg.create_task(
                    self._supervise(
                        "status-file-writer",
                        lambda: status_writer(self.state, self.config.status_interval),
                    )
                )

                if self.config.metrics_enabled:
                    tg.create_task(
                        self._supervise(
                            "metrics-publisher",
                            lambda: publish_metrics(self.state, self.config, self.service.enqueue_mqtt),
                        )
                    )

                    if self.config.bridge_summary_interval > 0:
                        tg.create_task(
                            self._supervise(
                                "bridge-snapshots",
                                lambda: publish_bridge_snapshots(self.state, self.config, self.service.enqueue_mqtt),
                            )
                        )

                    self.exporter = PrometheusExporter(
                        self.config.metrics_host,
                        self.config.metrics_port,
                    )
                    tg.create_task(self._supervise("prometheus-exporter", self.exporter.run))

                if self.config.watchdog_enabled:
                    self.watchdog = WatchdogKeepalive(interval=self.config.watchdog_interval, state=self.state)
                    tg.create_task(self._supervise("watchdog", self.watchdog.run))

        except* asyncio.CancelledError:
            logger.info("Daemon shutdown initiated.")
        except* SerialHandshakeFatal as exc:
            logger.critical("Fatal serial handshake error: %s", exc)
            sys.exit(1)
        except* Exception as exc_group:
            for exc in exc_group.exceptions:
                logger.critical("Fatal task error: %s", exc, exc_info=exc)
            sys.exit(1)
        finally:
            cleanup_status_file()
            self._cleanup()

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
        wait = tenacity.wait_exponential(multiplier=min_backoff, max=max_backoff)

        retryer = tenacity.AsyncRetrying(
            stop=stop,
            wait=wait,
            retry=tenacity.retry_if_exception_type(SUPERVISOR_RECOVERABLE_EXCEPTIONS),
            reraise=True,
        )

        try:
            async for attempt in retryer:
                with attempt:
                    logger.info("Starting task: %s", name)
                    await factory()
        except fatal_exceptions:
            logger.error("Task %s failed with fatal exception.", name)
            raise
        except Exception as exc:
            logger.error("Task %s exceeded retry limit: %s", name, exc)
            self.state.record_supervisor_failure(name, 0.0, exc)
            raise

    def _cleanup(self) -> None:
        """Final cleanup on daemon exit."""
        logger.info("Cleaning up MCU Bridge daemon resources...")


def main() -> None:
    """Daemon entry point."""
    import typer

    def _run_daemon() -> None:
        config = load_runtime_config()
        daemon = McuBridgeDaemon(config)
        try:
            asyncio.run(daemon.run())
        except KeyboardInterrupt:
            pass

    typer.run(_run_daemon)


if __name__ == "__main__":
    main()
