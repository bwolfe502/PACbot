"""Tests for quest rally tracking system (actions.py).

These functions were refactored to key by (device, quest_type) instead of
just quest_type, fixing multi-device state corruption. These tests verify
the fix works correctly.
"""
from unittest.mock import patch

from actions import (
    _track_quest_progress,
    _record_rally_started,
    _effective_remaining,
    reset_quest_tracking,
    _quest_rallies_pending,
    _quest_last_seen,
    _quest_pending_since,
)
from config import QuestType


# ============================================================
# _effective_remaining
# ============================================================

class TestEffectiveRemaining:
    def test_no_pending(self, mock_device):
        assert _effective_remaining(mock_device, QuestType.TITAN, 3, 15) == 12

    def test_some_pending(self, mock_device):
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 4
        assert _effective_remaining(mock_device, QuestType.TITAN, 3, 15) == 8

    def test_pending_exceeds_gap(self, mock_device):
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 20
        assert _effective_remaining(mock_device, QuestType.TITAN, 3, 15) == 0

    def test_already_complete(self, mock_device):
        assert _effective_remaining(mock_device, QuestType.TITAN, 15, 15) == 0

    def test_over_complete(self, mock_device):
        assert _effective_remaining(mock_device, QuestType.TITAN, 20, 15) == 0

    def test_devices_isolated(self, mock_device, mock_device_b):
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 5
        # Device B has no pending — should not see device A's state
        assert _effective_remaining(mock_device_b, QuestType.TITAN, 3, 15) == 12
        assert _effective_remaining(mock_device, QuestType.TITAN, 3, 15) == 7


# ============================================================
# _record_rally_started
# ============================================================

class TestRecordRallyStarted:
    def test_first_rally(self, mock_device):
        _record_rally_started(mock_device, QuestType.TITAN)
        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 1
        assert (mock_device, QuestType.TITAN) in _quest_pending_since

    def test_increments(self, mock_device):
        _record_rally_started(mock_device, QuestType.TITAN)
        _record_rally_started(mock_device, QuestType.TITAN)
        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 2

    def test_timestamp_only_set_on_first(self, mock_device):
        with patch("actions.time") as mock_time:
            mock_time.time.side_effect = [100.0, 200.0]
            _record_rally_started(mock_device, QuestType.TITAN)
            first_ts = _quest_pending_since[(mock_device, QuestType.TITAN)]
            _record_rally_started(mock_device, QuestType.TITAN)
            # Timestamp should NOT have been updated on second call
            assert _quest_pending_since[(mock_device, QuestType.TITAN)] == first_ts

    def test_different_quest_types(self, mock_device):
        _record_rally_started(mock_device, QuestType.TITAN)
        _record_rally_started(mock_device, QuestType.EVIL_GUARD)
        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 1
        assert _quest_rallies_pending[(mock_device, QuestType.EVIL_GUARD)] == 1

    def test_devices_isolated(self, mock_device, mock_device_b):
        _record_rally_started(mock_device, QuestType.TITAN)
        _record_rally_started(mock_device_b, QuestType.TITAN)
        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 1
        assert _quest_rallies_pending[(mock_device_b, QuestType.TITAN)] == 1


# ============================================================
# _track_quest_progress
# ============================================================

class TestTrackQuestProgress:
    def test_first_seen_sets_last_seen(self, mock_device):
        _track_quest_progress(mock_device, QuestType.TITAN, 5)
        assert _quest_last_seen[(mock_device, QuestType.TITAN)] == 5

    def test_counter_advance_reduces_pending(self, mock_device):
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 3
        _quest_last_seen[(mock_device, QuestType.TITAN)] = 5
        _quest_pending_since[(mock_device, QuestType.TITAN)] = 100.0
        _track_quest_progress(mock_device, QuestType.TITAN, 7)
        # Advanced by 2, so 3 - 2 = 1 pending
        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 1

    def test_counter_advance_clears_pending_to_zero(self, mock_device):
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 2
        _quest_last_seen[(mock_device, QuestType.TITAN)] = 5
        _quest_pending_since[(mock_device, QuestType.TITAN)] = 100.0
        _track_quest_progress(mock_device, QuestType.TITAN, 8)
        # Advanced by 3, pending was 2, so 0
        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 0
        # Timestamp should be cleared when pending hits 0
        assert (mock_device, QuestType.TITAN) not in _quest_pending_since

    def test_counter_backwards_resets(self, mock_device):
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 5
        _quest_last_seen[(mock_device, QuestType.TITAN)] = 10
        _quest_pending_since[(mock_device, QuestType.TITAN)] = 100.0
        _track_quest_progress(mock_device, QuestType.TITAN, 3)
        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 0
        assert (mock_device, QuestType.TITAN) not in _quest_pending_since

    def test_timeout_clears_pending(self, mock_device):
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 3
        _quest_last_seen[(mock_device, QuestType.TITAN)] = 5
        # Set pending_since to way in the past
        _quest_pending_since[(mock_device, QuestType.TITAN)] = 1.0

        with patch("actions.time") as mock_time:
            mock_time.time.return_value = 500.0  # 499s elapsed > 360s timeout
            _track_quest_progress(mock_device, QuestType.TITAN, 5)  # Same counter (no advance)

        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 0
        assert (mock_device, QuestType.TITAN) not in _quest_pending_since

    def test_no_timeout_when_within_window(self, mock_device):
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 3
        _quest_last_seen[(mock_device, QuestType.TITAN)] = 5
        _quest_pending_since[(mock_device, QuestType.TITAN)] = 100.0

        with patch("actions.time") as mock_time:
            mock_time.time.return_value = 200.0  # 100s elapsed < 360s timeout
            _track_quest_progress(mock_device, QuestType.TITAN, 5)

        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 3

    def test_devices_isolated(self, mock_device, mock_device_b):
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 3
        _quest_last_seen[(mock_device, QuestType.TITAN)] = 5
        _quest_pending_since[(mock_device, QuestType.TITAN)] = 100.0

        _quest_rallies_pending[(mock_device_b, QuestType.TITAN)] = 1
        _quest_last_seen[(mock_device_b, QuestType.TITAN)] = 5

        # Advance device A only
        _track_quest_progress(mock_device, QuestType.TITAN, 7)

        # Device A: 3 - 2 = 1
        assert _quest_rallies_pending[(mock_device, QuestType.TITAN)] == 1
        # Device B: still 1 (untouched)
        assert _quest_rallies_pending[(mock_device_b, QuestType.TITAN)] == 1


# ============================================================
# reset_quest_tracking
# ============================================================

class TestResetQuestTracking:
    def test_clear_all(self, mock_device, mock_device_b):
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 3
        _quest_rallies_pending[(mock_device_b, QuestType.EVIL_GUARD)] = 1
        _quest_last_seen[(mock_device, QuestType.TITAN)] = 5
        _quest_pending_since[(mock_device, QuestType.TITAN)] = 100.0

        reset_quest_tracking()

        assert len(_quest_rallies_pending) == 0
        assert len(_quest_last_seen) == 0
        assert len(_quest_pending_since) == 0

    def test_clear_single_device(self, mock_device, mock_device_b):
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 3
        _quest_rallies_pending[(mock_device, QuestType.EVIL_GUARD)] = 2
        _quest_rallies_pending[(mock_device_b, QuestType.TITAN)] = 1
        _quest_last_seen[(mock_device, QuestType.TITAN)] = 5
        _quest_last_seen[(mock_device_b, QuestType.TITAN)] = 8

        reset_quest_tracking(mock_device)

        # Device A: cleared
        assert (mock_device, QuestType.TITAN) not in _quest_rallies_pending
        assert (mock_device, QuestType.EVIL_GUARD) not in _quest_rallies_pending
        assert (mock_device, QuestType.TITAN) not in _quest_last_seen
        # Device B: untouched
        assert _quest_rallies_pending[(mock_device_b, QuestType.TITAN)] == 1
        assert _quest_last_seen[(mock_device_b, QuestType.TITAN)] == 8
