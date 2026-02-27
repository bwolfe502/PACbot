"""Tests for tower/fortress quest functions."""
import time
from unittest.mock import patch, MagicMock

import pytest

from config import QuestType, Screen
from actions import (
    _is_troop_defending, _navigate_to_tower, occupy_tower,
    recall_tower_troop, _run_tower_quest, _tower_quest_state,
)
from troops import TroopAction, TroopStatus, DeviceTroopSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_snapshot(device, actions):
    """Build a DeviceTroopSnapshot from a list of TroopAction values."""
    troops = [TroopStatus(action=a, seconds_remaining=60 if a != TroopAction.HOME else None)
              for a in actions]
    return DeviceTroopSnapshot(device=device, troops=troops)


# ---------------------------------------------------------------------------
# _is_troop_defending
# ---------------------------------------------------------------------------

class TestIsTroopDefending:
    def test_returns_true_when_defending(self, mock_device):
        snap = _make_snapshot(mock_device, [TroopAction.HOME, TroopAction.DEFENDING])
        with patch("actions.get_troop_status", return_value=snap):
            assert _is_troop_defending(mock_device) is True

    def test_returns_false_when_not_defending(self, mock_device):
        snap = _make_snapshot(mock_device, [TroopAction.HOME, TroopAction.RALLYING])
        with patch("actions.get_troop_status", return_value=snap):
            assert _is_troop_defending(mock_device) is False

    def test_returns_false_all_home(self, mock_device):
        snap = _make_snapshot(mock_device, [TroopAction.HOME, TroopAction.HOME])
        with patch("actions.get_troop_status", return_value=snap):
            assert _is_troop_defending(mock_device) is False

    def test_falls_back_to_panel_read_when_no_cache(self, mock_device):
        snap = _make_snapshot(mock_device, [TroopAction.DEFENDING])
        with patch("actions.get_troop_status", return_value=None), \
             patch("actions.read_panel_statuses", return_value=snap) as mock_read:
            assert _is_troop_defending(mock_device) is True
            mock_read.assert_called_once_with(mock_device)

    def test_falls_back_to_panel_read_when_cache_stale(self, mock_device):
        stale = _make_snapshot(mock_device, [TroopAction.DEFENDING])
        stale.read_at = time.time() - 60  # 60s old
        fresh = _make_snapshot(mock_device, [TroopAction.DEFENDING])
        with patch("actions.get_troop_status", return_value=stale), \
             patch("actions.read_panel_statuses", return_value=fresh) as mock_read:
            assert _is_troop_defending(mock_device) is True
            mock_read.assert_called_once()

    def test_returns_false_when_panel_read_fails(self, mock_device):
        with patch("actions.get_troop_status", return_value=None), \
             patch("actions.read_panel_statuses", return_value=None):
            assert _is_troop_defending(mock_device) is False


# ---------------------------------------------------------------------------
# _navigate_to_tower
# ---------------------------------------------------------------------------

class TestNavigateToTower:
    def test_success(self, mock_device):
        screen = MagicMock()
        with patch("actions.check_screen", return_value=Screen.MAP), \
             patch("actions.tap_image", return_value=True), \
             patch("actions.logged_tap"), \
             patch("actions.load_screenshot", return_value=screen), \
             patch("actions.find_image", return_value=(0.9, (100, 100), 50, 50)), \
             patch("actions.time.sleep"):
            assert _navigate_to_tower(mock_device) is True

    def test_no_target_marker(self, mock_device):
        t = [0.0]
        def fake_time():
            t[0] += 0.5
            return t[0]
        with patch("actions.check_screen", return_value=Screen.MAP), \
             patch("actions.tap_image", return_value=True), \
             patch("actions.logged_tap"), \
             patch("actions.load_screenshot", return_value=MagicMock()), \
             patch("actions.find_image", return_value=None), \
             patch("actions.time.sleep"), \
             patch("actions.time.time", side_effect=fake_time):
            assert _navigate_to_tower(mock_device) is False

    def test_target_menu_not_found(self, mock_device):
        with patch("actions.check_screen", return_value=Screen.MAP), \
             patch("actions.tap_image", return_value=False):
            assert _navigate_to_tower(mock_device) is False

    def test_navigates_to_map_first(self, mock_device):
        screen = MagicMock()
        with patch("actions.check_screen", return_value=Screen.UNKNOWN), \
             patch("actions.navigate", return_value=True) as mock_nav, \
             patch("actions.tap_image", return_value=True), \
             patch("actions.logged_tap"), \
             patch("actions.load_screenshot", return_value=screen), \
             patch("actions.find_image", return_value=(0.9, (100, 100), 50, 50)), \
             patch("actions.time.sleep"):
            assert _navigate_to_tower(mock_device) is True
            mock_nav.assert_called_once()


# ---------------------------------------------------------------------------
# occupy_tower
# ---------------------------------------------------------------------------

class TestOccupyTower:
    def test_skips_if_already_defending(self, mock_device):
        with patch("actions.navigate", return_value=True), \
             patch("actions._is_troop_defending", return_value=True):
            assert occupy_tower(mock_device) is True

    def test_fails_if_no_troops(self, mock_device):
        with patch("actions.navigate", return_value=True), \
             patch("actions._is_troop_defending", return_value=False), \
             patch("actions.troops_avail", return_value=0):
            assert occupy_tower(mock_device) is False

    def test_deploys_successfully(self, mock_device):
        with patch("actions.navigate", return_value=True), \
             patch("actions._is_troop_defending", return_value=False), \
             patch("actions.troops_avail", return_value=3), \
             patch("actions._navigate_to_tower", return_value=True), \
             patch("actions.logged_tap"), \
             patch("actions.tap_image", return_value=True), \
             patch("actions.config") as mock_config, \
             patch("actions.time.sleep"):
            mock_config.set_device_status = MagicMock()
            assert occupy_tower(mock_device) is True
            assert mock_device in _tower_quest_state

    def test_fails_if_tower_nav_fails(self, mock_device):
        with patch("actions.navigate", return_value=True), \
             patch("actions._is_troop_defending", return_value=False), \
             patch("actions.troops_avail", return_value=3), \
             patch("actions._navigate_to_tower", return_value=False), \
             patch("actions.config") as mock_config:
            mock_config.set_device_status = MagicMock()
            assert occupy_tower(mock_device) is False

    def test_fails_if_depart_not_found(self, mock_device):
        with patch("actions.navigate", return_value=True), \
             patch("actions._is_troop_defending", return_value=False), \
             patch("actions.troops_avail", return_value=3), \
             patch("actions._navigate_to_tower", return_value=True), \
             patch("actions.logged_tap"), \
             patch("actions.tap_image", return_value=False), \
             patch("actions.save_failure_screenshot"), \
             patch("actions.config") as mock_config, \
             patch("actions.time.sleep"):
            mock_config.set_device_status = MagicMock()
            assert occupy_tower(mock_device) is False

    def test_respects_stop_check(self, mock_device):
        stop = MagicMock(side_effect=[False, True])
        with patch("actions.navigate", return_value=True), \
             patch("actions._is_troop_defending", return_value=False):
            assert occupy_tower(mock_device, stop_check=stop) is False


# ---------------------------------------------------------------------------
# recall_tower_troop
# ---------------------------------------------------------------------------

class TestRecallTowerTroop:
    def test_full_recall_sequence(self, mock_device):
        _tower_quest_state[mock_device] = {"deployed_at": time.time()}
        with patch("actions.navigate", return_value=True), \
             patch("actions.tap_image", return_value=True), \
             patch("actions.logged_tap"), \
             patch("actions.config") as mock_config, \
             patch("actions.time.sleep"):
            mock_config.set_device_status = MagicMock()
            assert recall_tower_troop(mock_device) is True
            assert mock_device not in _tower_quest_state

    def test_fails_if_no_defending_icon(self, mock_device):
        with patch("actions.navigate", return_value=True), \
             patch("actions.tap_image", return_value=False), \
             patch("actions.config") as mock_config:
            mock_config.set_device_status = MagicMock()
            assert recall_tower_troop(mock_device) is False

    def test_fails_if_nav_fails(self, mock_device):
        with patch("actions.navigate", return_value=False):
            assert recall_tower_troop(mock_device) is False

    def test_respects_stop_check(self, mock_device):
        stop = MagicMock(side_effect=[False, True])
        with patch("actions.navigate", return_value=True), \
             patch("actions.tap_image", return_value=True), \
             patch("actions.logged_tap"), \
             patch("actions.config") as mock_config, \
             patch("actions.time.sleep"):
            mock_config.set_device_status = MagicMock()
            # Should abort mid-recall
            assert recall_tower_troop(mock_device, stop_check=stop) is False


# ---------------------------------------------------------------------------
# _run_tower_quest
# ---------------------------------------------------------------------------

class TestRunTowerQuest:
    def test_deploys_when_not_defending(self, mock_device):
        quests = [
            {"quest_type": QuestType.TOWER, "current": 0, "target": 30, "completed": False},
        ]
        with patch("actions._is_troop_defending", return_value=False), \
             patch("actions.occupy_tower", return_value=True) as mock_occ, \
             patch("actions.config") as mock_config:
            mock_config.set_device_status = MagicMock()
            _run_tower_quest(mock_device, quests)
            mock_occ.assert_called_once()

    def test_skips_when_already_defending(self, mock_device):
        quests = [
            {"quest_type": QuestType.FORTRESS, "current": 10, "target": 30, "completed": False},
        ]
        with patch("actions._is_troop_defending", return_value=True), \
             patch("actions.occupy_tower") as mock_occ, \
             patch("actions.config") as mock_config:
            mock_config.set_device_status = MagicMock()
            _run_tower_quest(mock_device, quests)
            mock_occ.assert_not_called()

    def test_recalls_when_all_complete(self, mock_device):
        quests = [
            {"quest_type": QuestType.TOWER, "current": 30, "target": 30, "completed": True},
        ]
        with patch("actions._is_troop_defending", return_value=True), \
             patch("actions.recall_tower_troop") as mock_recall:
            _run_tower_quest(mock_device, quests)
            mock_recall.assert_called_once()

    def test_no_recall_when_complete_but_not_defending(self, mock_device):
        quests = [
            {"quest_type": QuestType.TOWER, "current": 30, "target": 30, "completed": True},
        ]
        with patch("actions._is_troop_defending", return_value=False), \
             patch("actions.recall_tower_troop") as mock_recall:
            _run_tower_quest(mock_device, quests)
            mock_recall.assert_not_called()

    def test_handles_mixed_tower_and_fortress(self, mock_device):
        quests = [
            {"quest_type": QuestType.TOWER, "current": 5, "target": 30, "completed": False},
            {"quest_type": QuestType.FORTRESS, "current": 0, "target": 30, "completed": False},
        ]
        with patch("actions._is_troop_defending", return_value=False), \
             patch("actions.occupy_tower", return_value=True) as mock_occ, \
             patch("actions.config") as mock_config:
            mock_config.set_device_status = MagicMock()
            _run_tower_quest(mock_device, quests)
            mock_occ.assert_called_once()

    def test_no_tower_quests_does_nothing(self, mock_device):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 0, "target": 15, "completed": False},
        ]
        with patch("actions._is_troop_defending") as mock_def, \
             patch("actions.occupy_tower") as mock_occ, \
             patch("actions.recall_tower_troop") as mock_recall:
            _run_tower_quest(mock_device, quests)
            mock_def.assert_not_called()
            mock_occ.assert_not_called()
            mock_recall.assert_not_called()
