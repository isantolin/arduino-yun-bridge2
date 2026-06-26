from unittest.mock import MagicMock, patch


from mcubridge.state.storage import DbmCache, DbmDeque


def test_dbm_deque_recreate_on_corrupt() -> None:
    mock_db2 = MagicMock()
    side_effects = [OSError("Corrupt DB"), mock_db2]

    with (
        patch("dbm.open", side_effect=side_effects) as mock_open,
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink") as mock_unlink,
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):

        DbmDeque("mock_path")

        assert mock_open.call_count == 2
        mock_open.assert_any_call("mock_path", "c")
        mock_open.assert_any_call("mock_path", "n")

        assert mock_unlink.call_count == 5
        mock_logger.warning.assert_any_call("DbmDeque database corrupt or incomplete, recreating: %s", side_effects[0])


def test_dbm_deque_unlink_os_error() -> None:
    mock_db2 = MagicMock()
    side_effects = [OSError("Corrupt DB"), mock_db2]

    with (
        patch("dbm.open", side_effect=side_effects),
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink", side_effect=OSError("Permission denied")),
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):

        DbmDeque("mock_path")

        mock_logger.warning.assert_any_call(
            "Failed to unlink target path", path="mock_path", error=mock_logger.warning.call_args[1]["error"]
        )


def test_dbm_cache_recreate_on_corrupt() -> None:
    mock_db2 = MagicMock()
    side_effects = [OSError("Corrupt DB"), mock_db2]

    with (
        patch("dbm.open", side_effect=side_effects) as mock_open,
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink") as mock_unlink,
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):

        DbmCache("mock_path")

        assert mock_open.call_count == 2
        mock_open.assert_any_call("mock_path", "c")
        mock_open.assert_any_call("mock_path", "n")

        assert mock_unlink.call_count == 5
        mock_logger.warning.assert_any_call("DbmCache database corrupt or incomplete, recreating: %s", side_effects[0])


def test_dbm_cache_unlink_os_error() -> None:
    mock_db2 = MagicMock()
    side_effects = [OSError("Corrupt DB"), mock_db2]

    with (
        patch("dbm.open", side_effect=side_effects),
        patch("mcubridge.state.storage.Path.exists", return_value=True),
        patch("mcubridge.state.storage.Path.unlink", side_effect=OSError("Permission denied")),
        patch("mcubridge.state.storage.logger") as mock_logger,
    ):

        DbmCache("mock_path")

        mock_logger.warning.assert_any_call(
            "Failed to unlink target path", path="mock_path", error=mock_logger.warning.call_args[1]["error"]
        )
