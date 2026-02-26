"""Tests for device detection (devices.py)."""

import subprocess
from unittest.mock import patch, MagicMock

from devices import auto_connect_emulators, get_devices, get_emulator_instances


# ============================================================
# auto_connect_emulators
# ============================================================

class TestAutoConnectEmulators:
    @patch("devices.EMULATOR_PORTS", {"mumu": [7555, 7556]})
    @patch("devices.subprocess.run")
    def test_connected(self, mock_run):
        """Ports that respond with 'connected to' are returned."""
        mock_run.return_value = MagicMock(stdout="connected to 127.0.0.1:7555")
        result = auto_connect_emulators()
        assert "127.0.0.1:7555" in result

    @patch("devices.EMULATOR_PORTS", {"mumu": [7555]})
    @patch("devices.subprocess.run")
    def test_already_connected(self, mock_run):
        """'already connected' also counts as success."""
        mock_run.return_value = MagicMock(stdout="already connected to 127.0.0.1:7555")
        result = auto_connect_emulators()
        assert "127.0.0.1:7555" in result

    @patch("devices.EMULATOR_PORTS", {"mumu": [7555]})
    @patch("devices.subprocess.run")
    def test_timeout_skipped(self, mock_run):
        """Ports that time out are silently skipped."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="adb", timeout=3)
        result = auto_connect_emulators()
        assert result == []


# ============================================================
# get_devices
# ============================================================

class TestGetDevices:
    @patch("devices.subprocess.run")
    def test_single_device(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="List of devices attached\n127.0.0.1:7555\tdevice\n"
        )
        result = get_devices()
        assert result == ["127.0.0.1:7555"]

    @patch("devices.subprocess.run")
    def test_multiple_devices(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="List of devices attached\n127.0.0.1:7555\tdevice\n127.0.0.1:5555\tdevice\n"
        )
        result = get_devices()
        assert len(result) == 2

    @patch("devices.subprocess.run")
    def test_both_emulator_and_ip_returned(self, mock_run):
        """get_devices() returns all devices as-is (dedup is handled by
        skipping auto_connect on Windows instead)."""
        mock_run.return_value = MagicMock(
            stdout="List of devices attached\nemulator-5554\tdevice\n127.0.0.1:5555\tdevice\n"
        )
        result = get_devices()
        assert result == ["emulator-5554", "127.0.0.1:5555"]

    @patch("devices.subprocess.run")
    def test_empty(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="List of devices attached\n"
        )
        result = get_devices()
        assert result == []

    @patch("devices.subprocess.run")
    def test_subprocess_failure(self, mock_run):
        mock_run.side_effect = Exception("ADB not found")
        result = get_devices()
        assert result == []


# ============================================================
# get_emulator_instances
# ============================================================

class TestGetEmulatorInstances:
    @patch("devices.get_devices")
    @patch("devices.platform.system")
    def test_non_windows(self, mock_platform, mock_get_devices):
        """On Linux/macOS, returns device IDs as display names."""
        mock_platform.return_value = "Linux"
        mock_get_devices.return_value = ["127.0.0.1:7555"]
        result = get_emulator_instances()
        assert result == {"127.0.0.1:7555": "127.0.0.1:7555"}

    @patch("devices.get_devices")
    @patch("devices.platform.system")
    def test_empty_device_list(self, mock_platform, mock_get_devices):
        mock_platform.return_value = "Linux"
        mock_get_devices.return_value = []
        result = get_emulator_instances()
        assert result == {}

    @patch("devices._get_emulator_instances_windows")
    @patch("devices.get_devices")
    @patch("devices.platform.system")
    def test_windows_delegates(self, mock_platform, mock_get_devices, mock_win_func):
        """On Windows, delegates to _get_emulator_instances_windows."""
        mock_platform.return_value = "Windows"
        mock_get_devices.return_value = ["127.0.0.1:7555"]
        mock_win_func.return_value = {"127.0.0.1:7555": "MuMu Player 1"}
        result = get_emulator_instances()
        assert result == {"127.0.0.1:7555": "MuMu Player 1"}
        mock_win_func.assert_called_once_with(["127.0.0.1:7555"])
