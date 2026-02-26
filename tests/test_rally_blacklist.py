"""Tests for rally owner blacklist (actions.py).

When joining a rally fails (e.g. "Cannot march across protected zones"),
the rally owner is tracked. After RALLY_BLACKLIST_THRESHOLD consecutive
failures (or immediate error detection), the owner is blacklisted with
a time-based expiry.
"""
import time
import pytest
from unittest.mock import patch

from actions import (
    _blacklist_rally_owner,
    _is_rally_owner_blacklisted,
    _record_rally_owner_failure,
    _clear_rally_owner_failures,
    reset_rally_blacklist,
    _rally_owner_blacklist,
    _rally_owner_failures,
    RALLY_BLACKLIST_THRESHOLD,
    RALLY_BLACKLIST_EXPIRY_S,
)


# ============================================================
# Direct blacklisting (e.g. from error message detection)
# ============================================================

class TestDirectBlacklist:
    def test_not_blacklisted_by_default(self, mock_device):
        assert not _is_rally_owner_blacklisted(mock_device, "SomePlayer")

    def test_blacklist_then_check(self, mock_device):
        _blacklist_rally_owner(mock_device, "Mitzuzurama")
        assert _is_rally_owner_blacklisted(mock_device, "Mitzuzurama")

    def test_case_insensitive(self, mock_device):
        _blacklist_rally_owner(mock_device, "DrFate")
        assert _is_rally_owner_blacklisted(mock_device, "drfate")
        assert _is_rally_owner_blacklisted(mock_device, "DRFATE")
        assert _is_rally_owner_blacklisted(mock_device, "DrFate")

    @pytest.mark.parametrize("invalid_owner", ["", None])
    def test_invalid_owner_ignored(self, mock_device, invalid_owner):
        _blacklist_rally_owner(mock_device, invalid_owner)
        assert not _is_rally_owner_blacklisted(mock_device, invalid_owner)

    def test_whitespace_stripped(self, mock_device):
        _blacklist_rally_owner(mock_device, "  Bchen  ")
        assert _is_rally_owner_blacklisted(mock_device, "Bchen")
        assert _is_rally_owner_blacklisted(mock_device, "  Bchen  ")


# ============================================================
# Consecutive failure tracking
# ============================================================

class TestFailureTracking:
    def test_single_failure_not_blacklisted(self, mock_device):
        result = _record_rally_owner_failure(mock_device, "Player1")
        assert not result  # Not yet blacklisted
        assert not _is_rally_owner_blacklisted(mock_device, "Player1")
        assert _rally_owner_failures[mock_device]["player1"] == 1

    def test_threshold_failures_triggers_blacklist(self, mock_device):
        for i in range(RALLY_BLACKLIST_THRESHOLD - 1):
            assert not _record_rally_owner_failure(mock_device, "Player1")
        # This one should trigger blacklisting
        assert _record_rally_owner_failure(mock_device, "Player1")
        assert _is_rally_owner_blacklisted(mock_device, "Player1")

    def test_failure_counter_cleared_after_blacklist(self, mock_device):
        for _ in range(RALLY_BLACKLIST_THRESHOLD):
            _record_rally_owner_failure(mock_device, "Player1")
        # Failure counter should be cleared once blacklisted
        assert "player1" not in _rally_owner_failures.get(mock_device, {})

    def test_success_clears_failures(self, mock_device):
        _record_rally_owner_failure(mock_device, "Player1")
        assert _rally_owner_failures[mock_device]["player1"] == 1
        _clear_rally_owner_failures(mock_device, "Player1")
        assert "player1" not in _rally_owner_failures.get(mock_device, {})

    def test_success_clear_empty_is_safe(self, mock_device):
        # Should not raise
        _clear_rally_owner_failures(mock_device, "Nobody")
        _clear_rally_owner_failures(mock_device, "")
        _clear_rally_owner_failures(mock_device, None)

    @pytest.mark.parametrize("invalid_owner", ["", None])
    def test_invalid_owner_failure_ignored(self, mock_device, invalid_owner):
        assert not _record_rally_owner_failure(mock_device, invalid_owner)

    def test_different_owners_tracked_separately(self, mock_device):
        _record_rally_owner_failure(mock_device, "Player1")
        _record_rally_owner_failure(mock_device, "Player2")
        assert _rally_owner_failures[mock_device]["player1"] == 1
        assert _rally_owner_failures[mock_device]["player2"] == 1


# ============================================================
# Per-device isolation
# ============================================================

class TestBlacklistPerDevice:
    def test_different_devices_independent(self, mock_device, mock_device_b):
        _blacklist_rally_owner(mock_device, "BadPlayer")
        assert _is_rally_owner_blacklisted(mock_device, "BadPlayer")
        assert not _is_rally_owner_blacklisted(mock_device_b, "BadPlayer")

    def test_both_devices_blacklisted(self, mock_device, mock_device_b):
        _blacklist_rally_owner(mock_device, "BadPlayer")
        _blacklist_rally_owner(mock_device_b, "BadPlayer")
        assert _is_rally_owner_blacklisted(mock_device, "BadPlayer")
        assert _is_rally_owner_blacklisted(mock_device_b, "BadPlayer")

    def test_failure_counts_per_device(self, mock_device, mock_device_b):
        _record_rally_owner_failure(mock_device, "Player1")
        assert _rally_owner_failures.get(mock_device, {}).get("player1") == 1
        assert _rally_owner_failures.get(mock_device_b, {}).get("player1") is None


# ============================================================
# Time-based expiry
# ============================================================

class TestBlacklistExpiry:
    def test_not_expired_within_window(self, mock_device):
        _blacklist_rally_owner(mock_device, "Player1")
        assert _is_rally_owner_blacklisted(mock_device, "Player1")

    def test_expired_after_window(self, mock_device):
        _blacklist_rally_owner(mock_device, "Player1")
        # Simulate time passing beyond expiry
        _rally_owner_blacklist[mock_device]["player1"] = time.time() - RALLY_BLACKLIST_EXPIRY_S - 1
        assert not _is_rally_owner_blacklisted(mock_device, "Player1")

    def test_expired_entry_removed(self, mock_device):
        _blacklist_rally_owner(mock_device, "Player1")
        _rally_owner_blacklist[mock_device]["player1"] = time.time() - RALLY_BLACKLIST_EXPIRY_S - 1
        _is_rally_owner_blacklisted(mock_device, "Player1")
        # Entry should be cleaned up after expiry check
        assert "player1" not in _rally_owner_blacklist[mock_device]


# ============================================================
# reset_rally_blacklist
# ============================================================

class TestResetBlacklist:
    def test_reset_specific_device(self, mock_device, mock_device_b):
        _blacklist_rally_owner(mock_device, "Player1")
        _blacklist_rally_owner(mock_device_b, "Player2")
        reset_rally_blacklist(mock_device)
        assert not _is_rally_owner_blacklisted(mock_device, "Player1")
        assert _is_rally_owner_blacklisted(mock_device_b, "Player2")

    def test_reset_clears_failure_counters(self, mock_device):
        _record_rally_owner_failure(mock_device, "Player1")
        reset_rally_blacklist(mock_device)
        assert mock_device not in _rally_owner_failures

    def test_reset_all_devices(self, mock_device, mock_device_b):
        _blacklist_rally_owner(mock_device, "Player1")
        _blacklist_rally_owner(mock_device_b, "Player2")
        _record_rally_owner_failure(mock_device, "Player3")
        reset_rally_blacklist()
        assert not _is_rally_owner_blacklisted(mock_device, "Player1")
        assert not _is_rally_owner_blacklisted(mock_device_b, "Player2")
        assert _rally_owner_failures == {}

    def test_reset_nonexistent_device(self, mock_device):
        # Should not raise
        reset_rally_blacklist("nonexistent_device")

    def test_reset_already_empty(self, mock_device):
        # Should not raise
        reset_rally_blacklist(mock_device)
        reset_rally_blacklist()
