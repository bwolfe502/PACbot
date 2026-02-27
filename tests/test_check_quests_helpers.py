"""Tests for check_quests helper functions extracted in Phase 2.

Tests _deduplicate_quests (pure function) and _get_actionable_quests.
"""
from unittest.mock import patch

from config import QuestType
from actions import _deduplicate_quests, _get_actionable_quests, _all_quests_visually_complete, _quest_rallies_pending


# ============================================================
# _deduplicate_quests
# ============================================================

class TestDeduplicateQuests:
    def test_single_quest_unchanged(self):
        quests = [{"quest_type": QuestType.TITAN, "current": 3, "target": 15}]
        result = _deduplicate_quests(quests)
        assert len(result) == 1
        assert result[0]["current"] == 3

    def test_keeps_most_remaining(self):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 14, "target": 15},  # 1 remaining
            {"quest_type": QuestType.TITAN, "current": 0, "target": 5},    # 5 remaining
        ]
        result = _deduplicate_quests(quests)
        assert len(result) == 1
        assert result[0]["current"] == 0
        assert result[0]["target"] == 5

    def test_different_types_kept(self):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 0, "target": 15},
            {"quest_type": QuestType.EVIL_GUARD, "current": 1, "target": 3},
        ]
        result = _deduplicate_quests(quests)
        assert len(result) == 2

    def test_three_of_same_type(self):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 14, "target": 15},  # 1 remaining
            {"quest_type": QuestType.TITAN, "current": 10, "target": 15},  # 5 remaining
            {"quest_type": QuestType.TITAN, "current": 0, "target": 5},    # 5 remaining (tie)
        ]
        result = _deduplicate_quests(quests)
        assert len(result) == 1
        # Should keep one of the 5-remaining entries
        assert result[0]["target"] - result[0]["current"] == 5

    def test_empty_list(self):
        assert _deduplicate_quests([]) == []

    def test_mixed_types_with_duplicates(self):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 14, "target": 15},
            {"quest_type": QuestType.EVIL_GUARD, "current": 0, "target": 3},
            {"quest_type": QuestType.TITAN, "current": 0, "target": 5},
            {"quest_type": QuestType.EVIL_GUARD, "current": 2, "target": 3},
        ]
        result = _deduplicate_quests(quests)
        assert len(result) == 2
        types = {q["quest_type"] for q in result}
        assert types == {QuestType.TITAN, QuestType.EVIL_GUARD}

    def test_completed_quest_kept_if_most_remaining(self):
        """Even if current >= target, it should be kept if it's the only entry for that type."""
        quests = [{"quest_type": QuestType.PVP, "current": 5, "target": 5}]
        result = _deduplicate_quests(quests)
        assert len(result) == 1

    def test_non_actionable_types_still_deduped(self):
        quests = [
            {"quest_type": QuestType.GATHER, "current": 0, "target": 5},
            {"quest_type": QuestType.GATHER, "current": 3, "target": 5},
        ]
        result = _deduplicate_quests(quests)
        assert len(result) == 1
        assert result[0]["current"] == 0  # kept the one with 5 remaining


# ============================================================
# _get_actionable_quests
# ============================================================

class TestGetActionableQuests:
    def test_filters_completed(self, mock_device):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 15, "target": 15, "completed": True},
        ]
        assert _get_actionable_quests(mock_device, quests) == []

    def test_tower_fortress_are_actionable(self, mock_device):
        quests = [
            {"quest_type": QuestType.FORTRESS, "current": 0, "target": 30, "completed": False},
            {"quest_type": QuestType.TOWER, "current": 0, "target": 30, "completed": False},
        ]
        result = _get_actionable_quests(mock_device, quests)
        assert len(result) == 2

    def test_returns_actionable(self, mock_device):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 3, "target": 15, "completed": False},
            {"quest_type": QuestType.EVIL_GUARD, "current": 1, "target": 3, "completed": False},
        ]
        result = _get_actionable_quests(mock_device, quests)
        assert len(result) == 2

    def test_excludes_zero_effective_remaining(self, mock_device):
        # Simulate pending rallies covering all remaining
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 12
        quests = [
            {"quest_type": QuestType.TITAN, "current": 3, "target": 15, "completed": False},
        ]
        result = _get_actionable_quests(mock_device, quests)
        assert result == []

    def test_mixed_actionable_and_not(self, mock_device):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 15, "target": 15, "completed": True},  # done
            {"quest_type": QuestType.EVIL_GUARD, "current": 1, "target": 3, "completed": False},  # actionable
            {"quest_type": QuestType.GATHER, "current": 0, "target": 1000000, "completed": False},  # actionable
            {"quest_type": QuestType.FORTRESS, "current": 0, "target": 30, "completed": False},  # actionable
            {"quest_type": QuestType.PVP, "current": 0, "target": 1, "completed": False},  # actionable
        ]
        result = _get_actionable_quests(mock_device, quests)
        assert len(result) == 4
        types = {q["quest_type"] for q in result}
        assert types == {QuestType.EVIL_GUARD, QuestType.GATHER, QuestType.PVP, QuestType.FORTRESS}

    def test_empty_list(self, mock_device):
        assert _get_actionable_quests(mock_device, []) == []

    def test_none_quest_type_filtered(self, mock_device):
        quests = [
            {"quest_type": None, "current": 0, "target": 5, "completed": False},
        ]
        assert _get_actionable_quests(mock_device, quests) == []


# ============================================================
# _all_quests_visually_complete
# ============================================================

class TestAllQuestsVisuallyComplete:
    def test_all_complete(self, mock_device):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 15, "target": 15, "completed": False},
            {"quest_type": QuestType.EVIL_GUARD, "current": 3, "target": 3, "completed": False},
        ]
        assert _all_quests_visually_complete(mock_device, quests) is True

    def test_completed_flag_counts(self, mock_device):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 15, "target": 15, "completed": True},
        ]
        assert _all_quests_visually_complete(mock_device, quests) is True

    def test_incomplete_quest_blocks(self, mock_device):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 10, "target": 15, "completed": False},
        ]
        assert _all_quests_visually_complete(mock_device, quests) is False

    def test_ignores_pending_rallies(self, mock_device):
        """Gold should mine even when pending rallies exist — only visual matters."""
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 5
        quests = [
            {"quest_type": QuestType.TITAN, "current": 15, "target": 15, "completed": False},
        ]
        assert _all_quests_visually_complete(mock_device, quests) is True

    @patch("actions._is_troop_defending", return_value=True)
    def test_tower_ok_if_defending(self, mock_defending, mock_device):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 15, "target": 15, "completed": False},
            {"quest_type": QuestType.TOWER, "current": 10, "target": 30, "completed": False},
        ]
        assert _all_quests_visually_complete(mock_device, quests) is True

    @patch("actions._is_troop_defending", return_value=False)
    def test_tower_blocks_if_not_defending(self, mock_defending, mock_device):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 15, "target": 15, "completed": False},
            {"quest_type": QuestType.TOWER, "current": 10, "target": 30, "completed": False},
        ]
        assert _all_quests_visually_complete(mock_device, quests) is False

    @patch("actions._is_troop_defending", return_value=True)
    def test_fortress_ok_if_defending(self, mock_defending, mock_device):
        quests = [
            {"quest_type": QuestType.FORTRESS, "current": 5, "target": 30, "completed": False},
        ]
        assert _all_quests_visually_complete(mock_device, quests) is True

    def test_empty_quests_returns_true(self, mock_device):
        assert _all_quests_visually_complete(mock_device, []) is True

    def test_unknown_type_ignored(self, mock_device):
        quests = [
            {"quest_type": None, "current": 0, "target": 5, "completed": False},
        ]
        assert _all_quests_visually_complete(mock_device, quests) is True

    def test_mixed_complete_and_incomplete(self, mock_device):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 15, "target": 15, "completed": False},
            {"quest_type": QuestType.PVP, "current": 0, "target": 1, "completed": False},
        ]
        assert _all_quests_visually_complete(mock_device, quests) is False
