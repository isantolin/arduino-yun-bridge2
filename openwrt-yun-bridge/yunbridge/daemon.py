#!/usr/bin/env python3
"""Async orchestrator for the Arduino Yun Bridge v2 daemon."""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, cast
from collections.abc import Awaitable

from builtins import BaseExceptionGroup, ExceptionGroup

from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_never,
)

from yunbridge.config.logging import configure_logging
from yunbridge.config.settings import RuntimeConfig, load_runtime_config
from yunbridge.metrics import (
    PrometheusExporter,
    publish_bridge_snapshots,
    publish_metrics,
)
from yunbridge.services.runtime import (
    BridgeService,
    SerialHandshakeFatal,
)
from yunbridge.state.context import RuntimeState, create_runtime_state
from yunbridge.state.status import cleanup_status_file, status_writer
from yunbridge.transport import (
    build_mqtt_tls_context,
    mqtt_task,
    serial_reader_task,
    serial_sender_not_ready,
)
from yunbridge.watchdog import WatchdogKeepalive


logger = logging.getLogger("yunbridge")


@dataclass(slots=True)
class _SupervisedTaskSpec:
    name: str
    factory: Callable[[], Awaitable[None]]
    fatal_exceptions: tuple[type[BaseException], ...] = ()
    max_restarts: int | None = None
    restart_interval: float = 60.0
    min_backoff: float = 1.0
    max_backoff: float = 30.0


class _RetryableSupervisorError(Exception):
    """Sentinel exception to request another supervisor attempt."""

    def __init__(
        self,
        original: BaseException,
        *,
        reset_backoff: bool,
    ) -> None:
        super().__init__(str(original))
        self.original = original
        self.reset_backoff = reset_backoff


class _SupervisorWait:
    """Stateful wait strategy that allows backoff resets."""

    def __init__(self, *, min_delay: float, max_delay: float) -> None:
        self._min = max(0.1, min_delay)
        self._max = max(self._min, max_delay)
        self._streak = 0

    def __call__(self, retry_state: RetryCallState) -> float:
        outcome = retry_state.outcome
        reset_requested = False
        if outcome is not None and outcome.failed:
            exc = outcome.exception()
            if isinstance(exc, _RetryableSupervisorError):
                reset_requested = exc.reset_backoff

        if reset_requested or self._streak <= 0:
            self._streak = 1
        else:
            self._streak += 1

        delay = min(self._max, self._min * (2 ** (self._streak - 1)))
        return delay


async def _supervise_task(
    name: str,
    coro_factory: Callable[[], Awaitable[None]],
    *,
    fatal_exceptions: tuple[type[BaseException], ...] = (),
    min_backoff: float = 1.0,
    max_backoff: float = 30.0,
    state: RuntimeState | None = None,
    max_restarts: int | None = None,
    restart_interval: float = 60.0,
) -> None:
    """Run *coro_factory* restarting it on failures."""

    restart_window = max(1.0, restart_interval)
    restarts_in_window = 0
    window_started = time.monotonic()

    wait_strategy = _SupervisorWait(
        min_delay=min_backoff,
        max_delay=max_backoff,
    )

    def _before_sleep(retry_state: RetryCallState) -> None:
        outcome: Any = retry_state.outcome
        next_action: Any = retry_state.next_action
        if outcome is None or not getattr(outcome, "failed", False):
            return
        exc = outcome.exception()
        if not isinstance(exc, _RetryableSupervisorError):
            return
        sleep_value = None
        if next_action is not None:
            sleep_value = getattr(next_action, "sleep", None)
        delay = (
            float(sleep_value)
            if sleep_value is not None
            else max(0.1, min_backoff)
        )
        logger.warning(
            "%s task crashed; restarting in %.1fs",
            name,
            delay,
        )
        if state is not None:
            state.record_supervisor_failure(
                name,
                backoff=delay,
                exc=exc.original,
            )

    retryer = AsyncRetrying(
        retry=retry_if_exception_type(_RetryableSupervisorError),
        wait=wait_strategy,
        stop=stop_never,
        reraise=True,
        before_sleep=_before_sleep,
    )

    async for attempt in retryer:
        with attempt:
            started = time.monotonic()
            try:
                await coro_factory()
                logger.warning(
                    "%s task exited cleanly; supervisor exiting",
                    name,
                )
                if state is not None:
                    state.mark_supervisor_healthy(name)
                return
            except asyncio.CancelledError:
                logger.debug("%s supervisor cancelled", name)
                raise
            except fatal_exceptions as exc:
                logger.critical("%s task hit fatal error: %s", name, exc)
                if state is not None:
                    state.record_supervisor_failure(
                        name,
                        backoff=0.0,
                        exc=exc,
                        fatal=True,
                    )
                raise
            except Exception as exc:
                logger.exception("%s task crashed", name)

                restarts_in_window += 1
                now = time.monotonic()
                window_age = now - window_started
                if window_age > restart_window:
                    window_started = now
                    restarts_in_window = 1
                    window_age = 0.0

                if (
                    max_restarts is not None
                    and max_restarts > 0
                    and restarts_in_window > max_restarts
                ):
                    logger.critical(
                        "%s task exceeded %d restarts within %.1fs; aborting",
                        name,
                        max_restarts,
                        window_age,
                    )
                    if state is not None:
                        state.record_supervisor_failure(
                            name,
                            backoff=0.0,
                            exc=exc,
                            fatal=True,
                        )
                    raise

                elapsed = now - started
                reset_backoff = elapsed > max_backoff
                raise _RetryableSupervisorError(
                    exc,
                    reset_backoff=reset_backoff,
                ) from exc


async def main_async(config: RuntimeConfig) -> None:
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

    supervised_tasks: list[_SupervisedTaskSpec] = [
        _SupervisedTaskSpec(
            name="serial-link",
            factory=_serial_runner,
            fatal_exceptions=(SerialHandshakeFatal,),
        ),
        _SupervisedTaskSpec(
            name="mqtt-link",
            factory=_mqtt_runner,
        ),
        _SupervisedTaskSpec(
            name="status-writer",
            factory=_status_runner,
            max_restarts=5,
            restart_interval=120.0,
            max_backoff=10.0,
        ),
        _SupervisedTaskSpec(
            name="metrics-publisher",
            factory=_metrics_runner,
            max_restarts=5,
            restart_interval=120.0,
            max_backoff=10.0,
        ),
    ]

    if (
        config.bridge_summary_interval > 0.0
        or config.bridge_handshake_interval > 0.0
    ):
        supervised_tasks.append(
            _SupervisedTaskSpec(
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
            _SupervisedTaskSpec(
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
            _SupervisedTaskSpec(
                name="prometheus-exporter",
                factory=exporter.run,
                max_restarts=5,
                restart_interval=300.0,
            )
        )

    try:
        async with asyncio.TaskGroup() as task_group:
            for spec in supervised_tasks:
                task_group.create_task(
                    _supervise_task(
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
