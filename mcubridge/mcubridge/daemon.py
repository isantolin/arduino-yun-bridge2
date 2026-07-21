#!/usr/bin/env python3
"""Async orchestrator for the Arduino MCU Bridge v2 daemon.

This module contains the main entry point and orchestration logic for the
MCU Bridge daemon, which manages communication between OpenWrt Linux and
the Arduino MCU over serial and CLOUD.

[SIL-2 COMPLIANCE]
The daemon implements robust error handling:
- Deterministic startup (Fail-Fast on missing deps)
- Task supervision with automatic restart and backoff
- Fatal exception handling for unrecoverable serial errors
- Graceful shutdown on SIGTERM/SIGINT
- Status file cleanup on exit

Architecture:
    main() -> BridgeService -> TaskGroup
        ├── serial-link (SerialTransport)
        ├── cloud-link (cloud_task)
        ├── status-writer (status_writer)
        ├── metrics-publisher (publish_metrics)
        ├── bridge-snapshots (optional)
        ├── watchdog (optional)
        ├── prometheus-exporter (optional)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import tenacity

# [SIL-2] Deterministic Import: uvloop is MANDATORY for performance on OpenWrt.
import structlog
import uvloop

from mcubridge.config.logging import configure_logging
from mcubridge.config.const import DEFAULT_SERIAL_SHARED_SECRET
from mcubridge.config.settings import (
    get_config_source,
    load_runtime_config,
)
from mcubridge.security.security import verify_crypto_integrity
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport

logger = structlog.get_logger("mcubridge")


def app(args: list[str] | None = None) -> None:
    """CLI entry point for mcubridge daemon."""
    parser = argparse.ArgumentParser(description="Arduino MCU Bridge v2 Daemon")
    parser.add_argument("--version", action="version", version="mcubridge v2.x")
    parser.parse_args(args)

    service: BridgeService | None = None
    state: RuntimeState | None = None

    try:
        config = load_runtime_config()
        configure_logging(config)
        if not verify_crypto_integrity():
            logger.critical("CRYPTOGRAPHIC INTEGRITY CHECK FAILED! Aborting for security.")
            sys.exit(1)

        state = create_runtime_state(config)
        state.config_source = get_config_source()

        # [SIL-2] Strict Mode Security Gate
        if config.serial_shared_secret == DEFAULT_SERIAL_SHARED_SECRET:
            config.cloud_enabled = False
            logger.error("STRICT MODE: Cloud transport has been DISABLED for security.")

        # 1. Create Serial Transport
        serial_transport = SerialTransport(config, state, None)

        # 2. Create Service and link transport
        service = BridgeService(config, state, serial_transport)
        serial_transport.service = service

        if config.serial_shared_secret:
            logger.info("Security check passed: Shared secret is configured.")

        # [SIL-2] Unified entry point via asyncio.Runner (Python 3.11+)
        # This handles signal registration and loop lifecycle deterministically.
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            runner.run(service.run())

    except KeyboardInterrupt:
        logger.info("Daemon interrupted by user.")
    except (
        TimeoutError,
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        SerialHandshakeFatal,
        tenacity.RetryError,
    ) as exc:
        logger.critical("Fatal error: %s", exc, exc_info=not isinstance(exc, RuntimeError))
        sys.exit(1)
    except ExceptionGroup as exc_group:
        handled, unhandled = exc_group.split(
            (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                asyncio.TimeoutError,
                SerialHandshakeFatal,
                tenacity.RetryError,
            )
        )
        if handled is None:
            raise
        for exc in handled.exceptions:
            logger.critical("Fatal grouped error: %s", exc, exc_info=exc)
        if unhandled is not None:
            raise unhandled
        sys.exit(1)
    finally:
        if service is not None:
            service.cleanup()
        elif state is not None:
            state.cleanup()


if __name__ == "__main__":  # pragma: no cover
    app()
