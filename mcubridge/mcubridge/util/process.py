"""Process management utilities for McuBridge (SIL-2)."""

from __future__ import annotations

import contextlib
import structlog
import psutil

logger = structlog.get_logger("mcubridge.util.process")


def cleanup_process_tree(pid: int, timeout: float = 3.0) -> None:
    """[SIL-2] Reliably terminate a process and all its children.

    Uses psutil delegation for atomic tree traversal and signal mapping.
    """
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        all_procs = children + [parent]

        # 1. Terminate all
        for p in all_procs:
            with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
                p.terminate()

        # 2. Wait for termination
        _, alive = psutil.wait_procs(all_procs, timeout=timeout)

        # 3. Force kill survivors
        for p in alive:
            with contextlib.suppress(psutil.NoSuchProcess, ProcessLookupError):
                logger.warning("Force killing zombie process %d", p.pid)
                p.kill()

    except (psutil.NoSuchProcess, ProcessLookupError):
        pass
    except psutil.Error as e:
        logger.error("Error during process tree cleanup (pid=%d): %s", pid, e)


__all__ = ["cleanup_process_tree"]
