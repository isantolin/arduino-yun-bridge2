"""No-op pytest plugin shim to bypass the Home Assistant dependency."""
from __future__ import annotations

PYTEST_DONT_REWRITE = True


def pytest_configure(config):
    """Explicit hook so pytest treats this as a valid plugin."""
    # Intentionally empty: we just need to satisfy setuptools entry points
    # without importing the heavy Home Assistant stack.
    return None
