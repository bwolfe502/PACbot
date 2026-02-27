"""Tests for gather gold functionality."""

import pytest
from unittest.mock import patch, MagicMock

import config
from config import QuestType
from actions import gather_gold, gather_gold_loop, _get_actionable_quests


# ============================================================
# _get_actionable_quests — GATHER is now actionable
# ============================================================

class TestGatherActionable:
    def test_gather_is_actionable(self, mock_device):
        quests = [
            {"quest_type": QuestType.GATHER, "current": 500000, "target": 1000000, "completed": False},
        ]
        result = _get_actionable_quests(mock_device, quests)
        assert len(result) == 1
        assert result[0]["quest_type"] == QuestType.GATHER

    def test_completed_gather_not_actionable(self, mock_device):
        quests = [
            {"quest_type": QuestType.GATHER, "current": 1000000, "target": 1000000, "completed": True},
        ]
        result = _get_actionable_quests(mock_device, quests)
        assert result == []

    def test_gather_with_rally_types(self, mock_device):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 3, "target": 15, "completed": False},
            {"quest_type": QuestType.GATHER, "current": 0, "target": 1000000, "completed": False},
        ]
        result = _get_actionable_quests(mock_device, quests)
        assert len(result) == 2
        types = {q["quest_type"] for q in result}
        assert types == {QuestType.TITAN, QuestType.GATHER}


# ============================================================
# gather_gold — mocked vision/ADB
# ============================================================

class TestGatherGold:
    @patch("actions.load_screenshot", return_value=MagicMock())
    @patch("actions.heal_all")
    @patch("actions.troops_avail", return_value=0)
    @patch("actions.navigate", return_value=True)
    def test_returns_false_when_no_troops(self, mock_nav, mock_troops,
                                          mock_heal, mock_ss, mock_device):
        config.MIN_TROOPS_AVAILABLE = 0
        result = gather_gold(mock_device)
        assert result is False

    @patch("actions.load_screenshot", return_value=MagicMock())
    @patch("actions.heal_all")
    @patch("actions.troops_avail", return_value=5)
    @patch("actions.navigate", return_value=False)
    def test_returns_false_when_nav_fails(self, mock_nav, mock_troops,
                                           mock_heal, mock_ss, mock_device):
        config.MIN_TROOPS_AVAILABLE = 0
        result = gather_gold(mock_device)
        assert result is False

    @patch("actions.save_failure_screenshot")
    @patch("actions.find_image", return_value=None)
    @patch("actions._set_gather_level")
    @patch("actions.logged_tap")
    @patch("actions.timed_wait")
    @patch("actions.check_screen", return_value=config.Screen.MAP)
    @patch("actions.load_screenshot", return_value=MagicMock())
    @patch("actions.heal_all")
    @patch("actions.troops_avail", return_value=5)
    @patch("actions.navigate", return_value=True)
    def test_returns_false_when_depart_not_found(self, mock_nav, mock_troops,
                                                   mock_heal, mock_ss,
                                                   mock_check, mock_wait,
                                                   mock_tap, mock_set_level,
                                                   mock_find, mock_save_fail,
                                                   mock_device):
        config.MIN_TROOPS_AVAILABLE = 0
        result = gather_gold(mock_device)
        assert result is False
        mock_save_fail.assert_called_once()

    @patch("actions.adb_tap")
    @patch("actions._save_click_trail")
    @patch("actions.find_image")
    @patch("actions._set_gather_level")
    @patch("actions.logged_tap")
    @patch("actions.timed_wait")
    @patch("actions.check_screen", return_value=config.Screen.MAP)
    @patch("actions.load_screenshot", return_value=MagicMock())
    @patch("actions.heal_all")
    @patch("actions.troops_avail", return_value=5)
    @patch("actions.navigate", return_value=True)
    def test_returns_true_when_depart_found(self, mock_nav, mock_troops,
                                              mock_heal, mock_ss,
                                              mock_check, mock_wait,
                                              mock_tap, mock_set_level,
                                              mock_find, mock_trail,
                                              mock_adb_tap, mock_device):
        config.MIN_TROOPS_AVAILABLE = 0
        # find_image returns a depart match
        mock_find.return_value = (0.9, (500, 1500), 50, 200)
        result = gather_gold(mock_device)
        assert result is True

    @patch("actions.load_screenshot", return_value=MagicMock())
    @patch("actions.heal_all")
    @patch("actions.troops_avail", return_value=5)
    @patch("actions.navigate", return_value=True)
    def test_stop_check_after_nav(self, mock_nav, mock_troops, mock_heal,
                                   mock_ss, mock_device):
        config.MIN_TROOPS_AVAILABLE = 0
        result = gather_gold(mock_device, stop_check=lambda: True)
        assert result is False


# ============================================================
# gather_gold_loop
# ============================================================

class TestRunGatherLoop:
    @patch("actions.gather_gold")
    @patch("actions.troops_avail", return_value=5)
    def test_deploys_up_to_max(self, mock_troops, mock_gather, mock_device):
        config.GATHER_MAX_TROOPS = 3
        config.MIN_TROOPS_AVAILABLE = 0
        mock_gather.return_value = True
        result = gather_gold_loop(mock_device)
        assert result == 3
        assert mock_gather.call_count == 3

    @patch("actions.gather_gold")
    @patch("actions.troops_avail", side_effect=[5, 5, 1])
    def test_stops_when_not_enough_troops(self, mock_troops, mock_gather, mock_device):
        config.GATHER_MAX_TROOPS = 5
        config.MIN_TROOPS_AVAILABLE = 1
        mock_gather.return_value = True
        result = gather_gold_loop(mock_device)
        assert result == 2

    @patch("actions.gather_gold")
    @patch("actions.troops_avail", return_value=5)
    def test_stops_on_failure(self, mock_troops, mock_gather, mock_device):
        config.GATHER_MAX_TROOPS = 3
        config.MIN_TROOPS_AVAILABLE = 0
        mock_gather.side_effect = [True, False]
        result = gather_gold_loop(mock_device)
        assert result == 1

    @patch("actions.gather_gold")
    @patch("actions.troops_avail", return_value=5)
    def test_stop_check_honored(self, mock_troops, mock_gather, mock_device):
        config.GATHER_MAX_TROOPS = 3
        config.MIN_TROOPS_AVAILABLE = 0
        mock_gather.return_value = True
        call_count = [0]
        def stop_after_one():
            call_count[0] += 1
            return call_count[0] > 1
        result = gather_gold_loop(mock_device, stop_check=stop_after_one)
        assert result == 1

    @patch("actions.gather_gold")
    @patch("actions.troops_avail", return_value=5)
    def test_returns_zero_on_immediate_stop(self, mock_troops, mock_gather, mock_device):
        config.GATHER_MAX_TROOPS = 3
        config.MIN_TROOPS_AVAILABLE = 0
        result = gather_gold_loop(mock_device, stop_check=lambda: True)
        assert result == 0
        mock_gather.assert_not_called()
