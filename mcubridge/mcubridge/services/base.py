"""Base interfaces for service components. [Eradicated Indirection]"""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = structlog.get_logger("mcubridge.service")

# All service components have been refactored to use direct dependency injection
# via the svcs container in BridgeService.
