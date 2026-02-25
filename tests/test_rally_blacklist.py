"""Tests for rally owner blacklist (actions.py).

When joining a rally fails (e.g. "Cannot march across protected zones"),
the rally owner is blacklisted so the bot skips their rallies going forward.
"""
from actions import (
    _blacklist_rally_owner,
    _is_rally_owner_blacklisted,
    reset_rally_blacklist,
    _rally_owner_blacklist,
)


# ============================================================
# _is_rally_owner_blacklisted / _blacklist_rally_owner
# ============================================================

class TestBlacklistBasics:
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

    def test_empty_owner_ignored(self, mock_device):
        _blacklist_rally_owner(mock_device, "")
        assert not _is_rally_owner_blacklisted(mock_device, "")
        assert _rally_owner_blacklist == {}

    def test_none_owner_ignored(self, mock_device):
        _blacklist_rally_owner(mock_device, None)
        assert not _is_rally_owner_blacklisted(mock_device, None)

    def test_whitespace_stripped(self, mock_device):
        _blacklist_rally_owner(mock_device, "  Bchen  ")
        assert _is_rally_owner_blacklisted(mock_device, "Bchen")
        assert _is_rally_owner_blacklisted(mock_device, "  Bchen  ")


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


# ============================================================
# Failure count tracking
# ============================================================

class TestBlacklistFailureCount:
    def test_failure_count_increments(self, mock_device):
        _blacklist_rally_owner(mock_device, "Repeat")
        assert _rally_owner_blacklist[mock_device]["repeat"] == 1
        _blacklist_rally_owner(mock_device, "Repeat")
        assert _rally_owner_blacklist[mock_device]["repeat"] == 2

    def test_multiple_owners(self, mock_device):
        _blacklist_rally_owner(mock_device, "Player1")
        _blacklist_rally_owner(mock_device, "Player2")
        assert _is_rally_owner_blacklisted(mock_device, "Player1")
        assert _is_rally_owner_blacklisted(mock_device, "Player2")
        assert not _is_rally_owner_blacklisted(mock_device, "Player3")


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

    def test_reset_all_devices(self, mock_device, mock_device_b):
        _blacklist_rally_owner(mock_device, "Player1")
        _blacklist_rally_owner(mock_device_b, "Player2")
        reset_rally_blacklist()
        assert not _is_rally_owner_blacklisted(mock_device, "Player1")
        assert not _is_rally_owner_blacklisted(mock_device_b, "Player2")

    def test_reset_nonexistent_device(self, mock_device):
        # Should not raise
        reset_rally_blacklist("nonexistent_device")

    def test_reset_already_empty(self, mock_device):
        # Should not raise
        reset_rally_blacklist(mock_device)
        reset_rally_blacklist()
