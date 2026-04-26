"""Extra tests for FileComponent coverage."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import RuntimeState
from mcubridge.services.file import FileComponent


@pytest.fixture
def file_comp(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> FileComponent:
    serial_flow = MagicMock()
    # In original code, FileComponent requires mqtt_flow
    return FileComponent(runtime_config, runtime_state, serial_flow, MagicMock())


@pytest.mark.asyncio
async def test_file_refresh_storage_usage_handles_oserror(
    file_comp: FileComponent,
) -> None:
    with patch("shutil.disk_usage", side_effect=OSError("disk error")):
        # Accessing protected member for coverage validation
        await cast(Any, file_comp)._refresh_storage_usage()


@pytest.mark.asyncio
async def test_file_write_with_quota_large_warning(file_comp: FileComponent) -> None:
    # Test path for large file warning
    # We patch the logger object in the module where it is used.
    with patch("mcubridge.services.file.logger.warning") as mock_warn:
        # file_comp.config is a msgspec Struct, we cast for testing
        cast(Any, file_comp.config).file_write_max_bytes = 100
        # Accessing protected member for coverage validation
        # Pass a Path object as expected by the type hint
        await cast(Any, file_comp)._write_with_quota(Path("test.txt"), b"A" * 1000)
        assert mock_warn.called
