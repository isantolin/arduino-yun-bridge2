"""Extra tests for FileComponent coverage."""

from unittest.mock import MagicMock, patch
import pytest
from mcubridge.services.file import FileComponent


@pytest.fixture
def file_comp(runtime_config, runtime_state):
    serial_flow = MagicMock()
    return FileComponent(runtime_config, runtime_state, serial_flow)


@pytest.mark.asyncio
async def test_file_refresh_storage_usage_handles_oserror(file_comp):
    with patch("shutil.disk_usage", side_effect=OSError("disk error")):
        await file_comp._refresh_storage_usage()
        # Should catch and continue


@pytest.mark.asyncio
async def test_file_write_with_quota_large_warning(file_comp):
    # Test path for large file warning
    with patch("mcubridge.services.file.logger") as mock_logger:
        file_comp.config = MagicMock(file_write_max_bytes=100)
        await file_comp._write_with_quota("test.txt", b"A" * 1000)
        assert mock_logger.warning.called
