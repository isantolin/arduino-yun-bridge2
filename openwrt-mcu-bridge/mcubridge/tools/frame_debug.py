"""Deprecated developer-only module.

This helper is intentionally *not* shipped as part of the OpenWrt runtime.
Use the repository-local tool instead:

    python -m tools.frame_debug
"""

from __future__ import annotations


raise ImportError(
    "mcubridge.tools.frame_debug is a developer-only tool and is not shipped. "
    "Use 'python -m tools.frame_debug' from the repository checkout instead."
)
