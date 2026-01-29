import logging
from unittest.mock import MagicMock
import pytest

@pytest.fixture(autouse=True)
def logging_mock_level_fix():
    """ Ensure all handlers have a numeric level to avoid comparisons with MagicMock. """
    original_handlers = []
    # Capture existing loggers
    loggers = [logging.getLogger(name) for name in logging.root.manager.loggerDict]
    loggers.append(logging.getLogger()) # Root logger

    for logger in loggers:
        for handler in logger.handlers:
            if isinstance(handler.level, MagicMock):
                original_handlers.append((handler, handler.level))
                handler.level = logging.NOTSET

    yield

    # Restore (though usually not necessary for tests)
    for handler, level in original_handlers:
        handler.level = level
