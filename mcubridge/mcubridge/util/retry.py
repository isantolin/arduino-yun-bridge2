"""Centralised retry-strategy factories for serial-link operations.

All serial-related retry loops share the same exponential-backoff
constants (SERIAL_HANDSHAKE_BACKOFF_BASE / MAX).  Rather than
duplicating those configurations in every module, they are defined
once here and obtained via a single factory call.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import tenacity

from mcubridge.config.const import (
    SERIAL_HANDSHAKE_BACKOFF_BASE,
    SERIAL_HANDSHAKE_BACKOFF_MAX,
)

if TYPE_CHECKING:
    from prometheus_client import Counter


def before_sleep_with_metric(
    logger: logging.Logger,
    log_level: int,
    counter: Counter,
    label: str,
) -> Callable[[tenacity.RetryCallState], None]:
    """Compose a before_sleep callback that logs AND increments a labeled counter."""
    log_cb = tenacity.before_sleep_log(logger, log_level)

    def _callback(retry_state: tenacity.RetryCallState) -> None:
        log_cb(retry_state)
        counter.labels(component=label).inc()

    return _callback


def serial_exponential_retryer(
    *,
    max_attempts: int,
    retry: tenacity.retry_base,
    logger: logging.Logger,
    before_sleep: Callable[[tenacity.RetryCallState], None] | None = None,
    reraise: bool = True,
) -> tenacity.AsyncRetrying:
    """Exponential-backoff retryer using the standard serial constants."""
    return tenacity.AsyncRetrying(
        stop=tenacity.stop_after_attempt(max_attempts),
        wait=tenacity.wait_exponential(
            multiplier=SERIAL_HANDSHAKE_BACKOFF_BASE,
            max=SERIAL_HANDSHAKE_BACKOFF_MAX,
        ),
        retry=retry,
        before_sleep=before_sleep or tenacity.before_sleep_log(logger, logging.WARNING),
        reraise=reraise,
    )


def handshake_sync_retryer(
    *,
    max_attempts: int,
    logger: logging.Logger,
) -> tenacity.AsyncRetrying:
    """Exponential-backoff *with jitter* for handshake synchronisation.

    Uses ``retry_if_result(False)`` because intermediate failures are
    signalled by returning ``False`` rather than raising.
    """
    return tenacity.AsyncRetrying(
        stop=tenacity.stop_after_attempt(max_attempts),
        wait=tenacity.wait_exponential_jitter(
            initial=SERIAL_HANDSHAKE_BACKOFF_BASE,
            max=SERIAL_HANDSHAKE_BACKOFF_MAX,
            jitter=1.0,
        ),
        retry=tenacity.retry_if_result(lambda res: res is False),
        before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
        reraise=False,
    )
