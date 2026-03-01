"""Tests for per-device config overrides (config.py)."""

import pytest
import config


@pytest.fixture(autouse=True)
def reset_device_config():
    """Clear device overrides before and after each test."""
    config.clear_device_overrides()
    yield
    config.clear_device_overrides()


class TestGetDeviceConfig:
    """get_device_config() returns override when set, global otherwise."""

    def test_global_fallback_when_no_override(self, mock_device):
        config.AUTO_HEAL_ENABLED = True
        assert config.get_device_config(mock_device, "auto_heal") is True

    def test_override_wins_over_global(self, mock_device):
        config.AUTO_HEAL_ENABLED = True
        config.set_device_overrides(mock_device, {"auto_heal": False})
        assert config.get_device_config(mock_device, "auto_heal") is False

    def test_override_partial_keys_fallback(self, mock_device):
        """Override one key; others fall back to global."""
        config.AUTO_HEAL_ENABLED = True
        config.MIN_TROOPS_AVAILABLE = 2
        config.set_device_overrides(mock_device, {"auto_heal": False})
        assert config.get_device_config(mock_device, "auto_heal") is False
        assert config.get_device_config(mock_device, "min_troops") == 2

    def test_unknown_key_raises(self, mock_device):
        with pytest.raises(KeyError, match="Unknown device config key"):
            config.get_device_config(mock_device, "nonexistent_key")

    def test_different_devices_independent(self, mock_device, mock_device_b):
        config.MIN_TROOPS_AVAILABLE = 0
        config.set_device_overrides(mock_device, {"min_troops": 3})
        config.set_device_overrides(mock_device_b, {"min_troops": 1})
        assert config.get_device_config(mock_device, "min_troops") == 3
        assert config.get_device_config(mock_device_b, "min_troops") == 1

    def test_integer_override(self, mock_device):
        config.AP_GEM_LIMIT = 0
        config.set_device_overrides(mock_device, {"ap_gem_limit": 500})
        assert config.get_device_config(mock_device, "ap_gem_limit") == 500

    def test_string_override(self, mock_device):
        config.MY_TEAM_COLOR = "red"
        config.set_device_overrides(mock_device, {"my_team": "blue"})
        assert config.get_device_config(mock_device, "my_team") == "blue"


class TestGetDeviceEnemyTeams:
    """get_device_enemy_teams() derives enemies from per-device team."""

    def test_global_team(self, mock_device):
        config.MY_TEAM_COLOR = "red"
        enemies = config.get_device_enemy_teams(mock_device)
        assert "red" not in enemies
        assert set(enemies) == {"yellow", "green", "blue"}

    def test_override_team(self, mock_device):
        config.MY_TEAM_COLOR = "red"
        config.set_device_overrides(mock_device, {"my_team": "blue"})
        enemies = config.get_device_enemy_teams(mock_device)
        assert "blue" not in enemies
        assert set(enemies) == {"yellow", "green", "red"}


class TestSetClearOverrides:
    """set_device_overrides() and clear_device_overrides() manage state."""

    def test_set_overrides(self, mock_device):
        config.set_device_overrides(mock_device, {"auto_heal": False})
        assert config._DEVICE_CONFIG[mock_device] == {"auto_heal": False}

    def test_clear_overrides(self, mock_device, mock_device_b):
        config.set_device_overrides(mock_device, {"auto_heal": False})
        config.set_device_overrides(mock_device_b, {"min_troops": 3})
        config.clear_device_overrides()
        assert config._DEVICE_CONFIG == {}

    def test_overwrite_existing(self, mock_device):
        config.set_device_overrides(mock_device, {"auto_heal": False})
        config.set_device_overrides(mock_device, {"auto_heal": True, "min_troops": 1})
        assert config._DEVICE_CONFIG[mock_device] == {"auto_heal": True, "min_troops": 1}
