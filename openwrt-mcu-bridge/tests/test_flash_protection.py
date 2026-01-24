"""Test suite for Flash Protection mechanisms (SIL-2 Safety)."""
import unittest
from unittest.mock import patch
from mcubridge.config.settings import load_runtime_config


class TestFlashProtection(unittest.TestCase):
    def test_file_system_root_must_be_volatile(self):
        """Ensure file_system_root raises ValueError if not in /tmp."""
        unsafe_conf = {
            "file_system_root": "/etc/unsafe",
            "allow_non_tmp_paths": "0",
            "serial_shared_secret": "valid_secret_1234",
            "serial_baud": "9600",
            "serial_port": "/dev/ttyS0",
        }
        with patch("mcubridge.config.settings.get_uci_config", return_value=unsafe_conf):
            with self.assertRaises(ValueError) as cm:
                load_runtime_config()
            self.assertIn("FLASH PROTECTION", str(cm.exception))
            self.assertIn("file_system_root", str(cm.exception))

    def test_mqtt_spool_dir_must_be_volatile(self):
        """Ensure mqtt_spool_dir raises ValueError if not in /tmp."""
        unsafe_conf = {
            "mqtt_spool_dir": "/mnt/flash/spool",
            # Even with override allowed for FS root, spool MUST be safe
            "allow_non_tmp_paths": "1",
            "serial_shared_secret": "valid_secret_1234",
            "serial_baud": "9600",
            "serial_port": "/dev/ttyS0",
        }
        with patch("mcubridge.config.settings.get_uci_config", return_value=unsafe_conf):
            with self.assertRaises(ValueError) as cm:
                load_runtime_config()
            self.assertIn("FLASH PROTECTION", str(cm.exception))
            self.assertIn("mqtt_spool_dir", str(cm.exception))

    def test_override_flag_allows_unsafe_fs_root(self):
        """Ensure allow_non_tmp_paths=1 bypasses check for file_system_root."""
        unsafe_conf = {
            "file_system_root": "/etc/custom",
            "allow_non_tmp_paths": "1",
            "serial_shared_secret": "secure_secret_1234",
            "serial_baud": "9600",
            "serial_port": "/dev/ttyS0",
        }
        with patch("mcubridge.config.settings.get_uci_config", return_value=unsafe_conf):
            config = load_runtime_config()
            self.assertEqual(config.file_system_root, "/etc/custom")


if __name__ == "__main__":
    unittest.main()
