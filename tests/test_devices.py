"""Tests for device detection (devices.py)."""

import subprocess
import pytest
from unittest.mock import patch, MagicMock

from devices import (auto_connect_emulators, get_devices, get_emulator_instances,
                     _auto_connect_by_ports, _connect_ports)


# ============================================================
# auto_connect_emulators / _connect_ports / _auto_connect_by_ports
# ============================================================

class TestConnectPorts:
    """Tests for _connect_ports (shared by Windows and non-Windows paths)."""

    @pytest.mark.parametrize("output", [
        "connected to 127.0.0.1:7555",
        "already connected to 127.0.0.1:7555",
    ])
    @patch("devices.subprocess.run")
    def test_successful_connection(self, mock_run, output):
        """Both 'connected to' and 'already connected to' are success."""
        mock_run.return_value = MagicMock(stdout=output)
        result = _connect_ports({7555})
        assert "127.0.0.1:7555" in result

    @patch("devices.subprocess.run")
    def test_timeout_skipped(self, mock_run):
        """Ports that time out are silently skipped."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="adb", timeout=3)
        result = _connect_ports({7555})
        assert result == []


class TestAutoConnectByPorts:
    """Tests for _auto_connect_by_ports (macOS/Linux path)."""

    @patch("devices.EMULATOR_PORTS", {"mumu": [7555, 7556]})
    @patch("devices.subprocess.run")
    def test_probes_known_ports(self, mock_run):
        mock_run.return_value = MagicMock(stdout="connected to 127.0.0.1:7555")
        result = _auto_connect_by_ports()
        assert "127.0.0.1:7555" in result


class TestAutoConnectEmulators:
    """Tests for auto_connect_emulators dispatch logic."""

    @patch("devices.platform.system", return_value="Linux")
    @patch("devices.EMULATOR_PORTS", {"mumu": [7555]})
    @patch("devices.subprocess.run")
    def test_non_windows_probes_ports(self, mock_run, _mock_sys):
        """On non-Windows, probes known emulator ports."""
        mock_run.return_value = MagicMock(stdout="connected to 127.0.0.1:7555")
        result = auto_connect_emulators()
        assert "127.0.0.1:7555" in result

    @patch("devices.platform.system", return_value="Windows")
    @patch("devices._auto_connect_windows")
    def test_windows_delegates(self, mock_win, _mock_sys):
        """On Windows, delegates to _auto_connect_windows."""
        mock_win.return_value = ["127.0.0.1:5635"]
        result = auto_connect_emulators()
        assert result == ["127.0.0.1:5635"]
        mock_win.assert_called_once()


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
    def test_duplicate_emulator_and_ip_deduplicated(self, mock_run):
        """emulator-5554 and 127.0.0.1:5555 are the same device (port 5554+1),
        so get_devices() keeps only the emulator-N form."""
        mock_run.return_value = MagicMock(
            stdout="List of devices attached\nemulator-5554\tdevice\n127.0.0.1:5555\tdevice\n"
        )
        result = get_devices()
        assert result == ["emulator-5554"]

    @patch("devices.subprocess.run")
    def test_non_overlapping_ip_kept(self, mock_run):
        """127.0.0.1:<port> entries that don't overlap with emulator-N are kept."""
        mock_run.return_value = MagicMock(
            stdout="List of devices attached\nemulator-5554\tdevice\n127.0.0.1:7555\tdevice\n"
        )
        result = get_devices()
        assert result == ["emulator-5554", "127.0.0.1:7555"]

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
