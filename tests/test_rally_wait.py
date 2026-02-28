"""Tests for the troop-tracked rally system.

Covers:
- Slot tracking in _record_rally_started / _track_quest_progress
- _wait_for_rallies panel-based waiting + false positive detection
- Feature toggle gating
"""

import time
from unittest.mock import patch, MagicMock

import pytest

from config import QuestType
from actions.quests import (
    _record_rally_started, _track_quest_progress, _effective_remaining,
    reset_quest_tracking, _wait_for_rallies,
    _quest_rallies_pending, _quest_last_seen, _quest_pending_since,
    _quest_rally_slots,
)
from actions._helpers import _last_depart_slot
from troops import TroopAction, TroopStatus, DeviceTroopSnapshot


# ── helpers ──────────────────────────────────────────────────────────

def _make_snapshot(device, actions):
    """Build a DeviceTroopSnapshot from a list of TroopActions."""
    troops = [TroopStatus(action=a) for a in actions]
    return DeviceTroopSnapshot(device=device, troops=troops)


# ── Slot tracking ────────────────────────────────────────────────────

class TestSlotTracking:

    def test_record_rally_stores_slot(self, mock_device):
        """_last_depart_slot is consumed and stored in _quest_rally_slots."""
        _last_depart_slot[mock_device] = 3
        _record_rally_started(mock_device, QuestType.TITAN)
        assert _quest_rally_slots[(mock_device, QuestType.TITAN)] == [3]
        assert mock_device not in _last_depart_slot  # consumed

    def test_record_rally_no_slot(self, mock_device):
        """Without a slot, pending still increments, slots list not created."""
        _record_rally_started(mock_device, QuestType.TITAN)
        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 1
        assert (mock_device, QuestType.TITAN) not in _quest_rally_slots

    def test_multiple_slots_tracked(self, mock_device):
        """Multiple rally starts append slots in order."""
        for slot in [1, 3, 5]:
            _last_depart_slot[mock_device] = slot
            _record_rally_started(mock_device, QuestType.EVIL_GUARD)
        assert _quest_rally_slots[(mock_device, QuestType.EVIL_GUARD)] == [1, 3, 5]
        assert _quest_rallies_pending[(mock_device, QuestType.EVIL_GUARD)] == 3

    def test_progress_pops_oldest_slots(self, mock_device):
        """When counter advances, oldest slots are removed."""
        _last_depart_slot[mock_device] = 1
        _record_rally_started(mock_device, QuestType.TITAN)
        _last_depart_slot[mock_device] = 3
        _record_rally_started(mock_device, QuestType.TITAN)
        # Simulate counter advancing by 1 (one rally completed)
        _quest_last_seen[(mock_device, QuestType.TITAN)] = 2
        _track_quest_progress(mock_device, QuestType.TITAN, 3)
        assert _quest_rally_slots[(mock_device, QuestType.TITAN)] == [3]  # slot 1 popped

    def test_progress_pops_multiple_slots(self, mock_device):
        """Counter advancing by N pops N oldest slots."""
        for slot in [1, 2, 3]:
            _last_depart_slot[mock_device] = slot
            _record_rally_started(mock_device, QuestType.TITAN)
        _quest_last_seen[(mock_device, QuestType.TITAN)] = 0
        _track_quest_progress(mock_device, QuestType.TITAN, 2)  # 2 completed
        assert _quest_rally_slots[(mock_device, QuestType.TITAN)] == [3]

    def test_reset_clears_slots_all(self):
        """reset_quest_tracking(None) clears all slot tracking dicts."""
        _last_depart_slot["dev1"] = 2
        _quest_rally_slots[("dev1", QuestType.TITAN)] = [1, 2]
        reset_quest_tracking()
        assert not _last_depart_slot
        assert not _quest_rally_slots

    def test_reset_clears_slots_per_device(self, mock_device, mock_device_b):
        """reset_quest_tracking(device) clears only that device."""
        _last_depart_slot[mock_device] = 1
        _last_depart_slot[mock_device_b] = 2
        _quest_rally_slots[(mock_device, QuestType.TITAN)] = [1]
        _quest_rally_slots[(mock_device_b, QuestType.TITAN)] = [2]
        reset_quest_tracking(mock_device)
        assert mock_device not in _last_depart_slot
        assert mock_device_b in _last_depart_slot
        assert (mock_device, QuestType.TITAN) not in _quest_rally_slots
        assert _quest_rally_slots[(mock_device_b, QuestType.TITAN)] == [2]

    def test_device_isolation(self, mock_device, mock_device_b):
        """Slots are tracked per device independently."""
        _last_depart_slot[mock_device] = 1
        _record_rally_started(mock_device, QuestType.TITAN)
        _last_depart_slot[mock_device_b] = 3
        _record_rally_started(mock_device_b, QuestType.TITAN)
        assert _quest_rally_slots[(mock_device, QuestType.TITAN)] == [1]
        assert _quest_rally_slots[(mock_device_b, QuestType.TITAN)] == [3]


# ── _wait_for_rallies ───────────────────────────────────────────────

class TestWaitForRallies:

    @patch("actions.quests.stats")
    @patch("actions.quests.time")
    @patch("actions.quests.read_panel_statuses")
    def test_no_rallying_clears_pending(self, mock_panel, mock_time, mock_stats, mock_device):
        """No rallying troops + pending > 0 → clears pending (false positive)."""
        # Extra time.time() calls from _interruptible_sleep loop
        mock_time.time.side_effect = [0.0, 5.0, 10.0, 10.0, 10.0]
        mock_time.sleep = MagicMock()
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 1
        _quest_pending_since[(mock_device, QuestType.TITAN)] = 0.0
        # Two consecutive reads with no rallying (confirmation read)
        mock_panel.side_effect = [
            _make_snapshot(mock_device, [TroopAction.HOME] * 5),
            _make_snapshot(mock_device, [TroopAction.HOME] * 5),
        ]
        _wait_for_rallies(mock_device, lambda: False)
        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 0
        mock_stats.record_action.assert_called_once()
        call_args = mock_stats.record_action.call_args
        assert call_args[0][1] == "rally_false_positive_cleared"

    @patch("actions.quests.stats")
    @patch("actions.quests.time")
    @patch("actions.quests.read_panel_statuses")
    def test_rallying_then_complete(self, mock_panel, mock_time, mock_stats, mock_device):
        """Rallying troop found → polls → rallying count drops → returns."""
        # Extra time.time() calls from _interruptible_sleep loop
        mock_time.time.side_effect = [0.0, 0.0, 5.0, 5.0, 10.0, 10.0, 10.0]
        mock_time.sleep = MagicMock()
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 1
        mock_panel.side_effect = [
            # Initial read: 1 rallying
            _make_snapshot(mock_device, [TroopAction.RALLYING, TroopAction.HOME,
                                         TroopAction.HOME, TroopAction.HOME, TroopAction.HOME]),
            # After poll: rallying completed → now returning
            _make_snapshot(mock_device, [TroopAction.RETURNING, TroopAction.HOME,
                                         TroopAction.HOME, TroopAction.HOME, TroopAction.HOME]),
        ]
        _wait_for_rallies(mock_device, lambda: False)
        # Pending NOT cleared here (check_quests re-reads counters to do that)
        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 1
        mock_stats.record_action.assert_called_once()
        call_args = mock_stats.record_action.call_args
        assert call_args[0][1] == "wait_for_rallies"

    @patch("actions.quests.stats")
    @patch("actions.quests.read_panel_statuses", return_value=None)
    def test_panel_failure_fallback(self, mock_panel, mock_stats, mock_device):
        """Panel read fails → returns without clearing pending (old behavior resumes)."""
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 1
        _wait_for_rallies(mock_device, lambda: False)
        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 1  # unchanged
        mock_stats.record_action.assert_not_called()

    @patch("actions.quests.stats")
    @patch("actions.quests.read_panel_statuses")
    def test_stop_check_exits_immediately(self, mock_panel, mock_stats, mock_device):
        """stop_check returning True exits immediately without modifying state."""
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 1
        mock_panel.return_value = _make_snapshot(
            mock_device, [TroopAction.RALLYING, TroopAction.HOME,
                          TroopAction.HOME, TroopAction.HOME, TroopAction.HOME])
        _wait_for_rallies(mock_device, lambda: True)  # stop immediately
        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 1

    @patch("actions.quests.stats")
    @patch("actions.quests.time")
    @patch("actions.quests.read_panel_statuses")
    def test_timeout_safety(self, mock_panel, mock_time, mock_stats, mock_device):
        """Polls until QUEST_PENDING_TIMEOUT then gives up."""
        # Extra time.time() calls from _interruptible_sleep loop
        mock_time.time.side_effect = [0.0, 0.0, 400.0, 405.0, 400.0]
        mock_time.sleep = MagicMock()
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 1
        mock_panel.return_value = _make_snapshot(
            mock_device, [TroopAction.RALLYING, TroopAction.HOME,
                          TroopAction.HOME, TroopAction.HOME, TroopAction.HOME])
        _wait_for_rallies(mock_device, lambda: False)
        # Returns without crash, pending still set
        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 1

    @patch("actions.quests.stats")
    @patch("actions.quests.time")
    @patch("actions.quests.read_panel_statuses")
    def test_no_rallying_single_transient_does_not_clear(self, mock_panel, mock_time, mock_stats, mock_device):
        """First read has no rallying, second does — don't clear (transient miss)."""
        # Extra time.time() calls from _interruptible_sleep loop
        mock_time.time.side_effect = [0.0, 5.0, 10.0, 10.0]
        mock_time.sleep = MagicMock()
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 1
        mock_panel.side_effect = [
            # First read: no rallying (transient miss)
            _make_snapshot(mock_device, [TroopAction.HOME] * 5),
            # Confirmation read: rallying found!
            _make_snapshot(mock_device, [TroopAction.RALLYING, TroopAction.HOME,
                                         TroopAction.HOME, TroopAction.HOME, TroopAction.HOME]),
        ]
        _wait_for_rallies(mock_device, lambda: False)
        # Should NOT have cleared pending (confirmation read found rallying)
        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 1

    @patch("actions.quests.stats")
    @patch("actions.quests.time")
    @patch("actions.quests.read_panel_statuses")
    def test_panel_fail_during_poll_falls_back(self, mock_panel, mock_time, mock_stats, mock_device):
        """Panel read fails during poll loop → returns gracefully."""
        # Extra time.time() calls from _interruptible_sleep loop
        mock_time.time.side_effect = [0.0, 0.0, 5.0, 10.0, 10.0]
        mock_time.sleep = MagicMock()
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 1
        mock_panel.side_effect = [
            # Initial: rallying found
            _make_snapshot(mock_device, [TroopAction.RALLYING, TroopAction.HOME,
                                         TroopAction.HOME, TroopAction.HOME, TroopAction.HOME]),
            # Next poll: panel fails
            None,
        ]
        _wait_for_rallies(mock_device, lambda: False)
        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 1

    @patch("actions.quests.stats")
    @patch("actions.quests.time")
    @patch("actions.quests.read_panel_statuses")
    def test_rallying_count_increases_updates_baseline(self, mock_panel, mock_time, mock_stats, mock_device):
        """If another rally joins while waiting, baseline updates."""
        # Extra time.time() calls from _interruptible_sleep loops (2 poll iterations)
        mock_time.time.side_effect = [0.0, 0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 25.0]
        mock_time.sleep = MagicMock()
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 1
        mock_panel.side_effect = [
            # Initial: 1 rallying
            _make_snapshot(mock_device, [TroopAction.RALLYING, TroopAction.HOME,
                                         TroopAction.HOME, TroopAction.HOME, TroopAction.HOME]),
            # Next poll: 2 rallying (another joined)
            _make_snapshot(mock_device, [TroopAction.RALLYING, TroopAction.RALLYING,
                                         TroopAction.HOME, TroopAction.HOME, TroopAction.HOME]),
            # Next poll: 1 rallying (first completed)
            _make_snapshot(mock_device, [TroopAction.RALLYING, TroopAction.HOME,
                                         TroopAction.HOME, TroopAction.HOME, TroopAction.HOME]),
        ]
        _wait_for_rallies(mock_device, lambda: False)
        # Detected completion (2→1)
        mock_stats.record_action.assert_called_once()


# ── Feature toggle ───────────────────────────────────────────────────

class TestToggle:

    @patch("actions.quests._wait_for_rallies")
    @patch("actions.quests._get_actionable_quests", return_value=[])
    @patch("actions.quests._deduplicate_quests", side_effect=lambda q: q)
    @patch("actions.quests._ocr_quest_rows")
    @patch("actions.quests._claim_quest_rewards", return_value=None)
    @patch("actions.quests.navigate", return_value=True)
    @patch("actions.quests.config")
    def test_toggle_off_skips_panel_wait(self, mock_config, mock_nav,
                                          mock_claim, mock_ocr, mock_dedup,
                                          mock_actionable, mock_wait, mock_device):
        """When RALLY_PANEL_WAIT_ENABLED is False, _wait_for_rallies is never called."""
        mock_config.RALLY_PANEL_WAIT_ENABLED = False
        mock_config.QUEST_PENDING_TIMEOUT = 360
        # Set up pending rallies so the code enters the waiting branch
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 1
        mock_ocr.return_value = [
            {"quest_type": QuestType.TITAN, "current": 0, "target": 5, "completed": False}
        ]
        from actions import check_quests
        check_quests(mock_device)
        mock_wait.assert_not_called()

    @patch("actions.quests._wait_for_rallies")
    @patch("actions.quests._get_actionable_quests", return_value=[])
    @patch("actions.quests._deduplicate_quests", side_effect=lambda q: q)
    @patch("actions.quests._ocr_quest_rows")
    @patch("actions.quests._claim_quest_rewards", return_value=None)
    @patch("actions.quests.navigate", return_value=True)
    @patch("actions.quests.config")
    def test_toggle_on_calls_panel_wait(self, mock_config, mock_nav,
                                         mock_claim, mock_ocr, mock_dedup,
                                         mock_actionable, mock_wait, mock_device):
        """When RALLY_PANEL_WAIT_ENABLED is True, _wait_for_rallies is called."""
        mock_config.RALLY_PANEL_WAIT_ENABLED = True
        mock_config.QUEST_PENDING_TIMEOUT = 360
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 1
        mock_ocr.return_value = [
            {"quest_type": QuestType.TITAN, "current": 0, "target": 5, "completed": False}
        ]
        from actions import check_quests
        check_quests(mock_device)
        mock_wait.assert_called_once()
