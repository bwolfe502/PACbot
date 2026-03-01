"""Tests for check_quests helper functions extracted in Phase 2.

Tests _deduplicate_quests (pure function) and _get_actionable_quests.
"""
import time
from unittest.mock import patch, MagicMock, call

from config import QuestType
from actions.quests import (_deduplicate_quests, _get_actionable_quests,
                            _all_quests_visually_complete, _quest_rallies_pending,
                            check_quests, _quest_last_seen, _quest_target,
                            _attack_pvp_tower, _pvp_last_dispatch, _PVP_COOLDOWN_S)


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

    @patch("actions.quests._is_troop_defending", return_value=True)
    def test_tower_ok_if_defending(self, mock_defending, mock_device):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 15, "target": 15, "completed": False},
            {"quest_type": QuestType.TOWER, "current": 10, "target": 30, "completed": False},
        ]
        assert _all_quests_visually_complete(mock_device, quests) is True

    @patch("actions.quests._is_troop_defending", return_value=False)
    def test_tower_blocks_if_not_defending(self, mock_defending, mock_device):
        quests = [
            {"quest_type": QuestType.TITAN, "current": 15, "target": 15, "completed": False},
            {"quest_type": QuestType.TOWER, "current": 10, "target": 30, "completed": False},
        ]
        assert _all_quests_visually_complete(mock_device, quests) is False

    @patch("actions.quests._is_troop_defending", return_value=True)
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


# ============================================================
# check_quests: gather blocked by pending rallies
# ============================================================

class TestGatherBlockedByPendingRallies:
    """Gather gold should NOT deploy while titan/EG rallies are in flight."""

    @patch("actions.quests._run_tower_quest")
    @patch("actions.farming.gather_gold_loop")
    @patch("actions.quests.navigate", return_value=True)
    @patch("actions.quests._claim_quest_rewards", return_value=0)
    @patch("actions.quests._ocr_quest_rows")
    def test_gather_blocked_when_titan_pending(self, mock_ocr, mock_claim,
                                                mock_nav, mock_gather,
                                                mock_tower, mock_device):
        """When titan has pending rallies and gather is actionable, should wait not gather."""
        # Titan at 18/20 with 2 pending rallies -> effective remaining 0
        _quest_rallies_pending[(mock_device, QuestType.TITAN)] = 2
        _quest_last_seen[(mock_device, QuestType.TITAN)] = 18
        _quest_target[(mock_device, QuestType.TITAN)] = 20

        mock_ocr.return_value = [
            {"quest_type": QuestType.TITAN, "current": 18, "target": 20, "completed": False,
             "text": "Defeat Titans(18/20)"},
            {"quest_type": QuestType.GATHER, "current": 0, "target": 1000000, "completed": False,
             "text": "Gather(0/200,000)"},
        ]

        with patch("actions.quests._wait_for_rallies") as mock_wait:
            check_quests(mock_device)
            # Should wait for rallies, NOT gather
            mock_wait.assert_called_once()
            mock_gather.assert_not_called()

    @patch("actions.quests._run_tower_quest")
    @patch("actions.farming.gather_gold_loop")
    @patch("actions.quests.navigate", return_value=True)
    @patch("actions.quests._claim_quest_rewards", return_value=0)
    @patch("actions.quests._ocr_quest_rows")
    def test_gather_proceeds_when_no_pending(self, mock_ocr, mock_claim,
                                              mock_nav, mock_gather,
                                              mock_tower, mock_device):
        """When no pending rallies, gather should proceed normally."""
        mock_ocr.return_value = [
            {"quest_type": QuestType.GATHER, "current": 0, "target": 1000000, "completed": False,
             "text": "Gather(0/200,000)"},
        ]

        check_quests(mock_device)
        mock_gather.assert_called_once()

    @patch("actions.quests._attack_pvp_tower")
    @patch("actions.quests._wait_for_rallies")
    @patch("actions.quests._run_tower_quest")
    @patch("actions.farming.gather_gold_loop")
    @patch("actions.quests.navigate", return_value=True)
    @patch("actions.quests._claim_quest_rewards", return_value=0)
    @patch("actions.quests._ocr_quest_rows")
    def test_pvp_dispatches_then_waits_when_rallies_pending(self, mock_ocr, mock_claim,
                                                              mock_nav, mock_gather,
                                                              mock_tower, mock_wait,
                                                              mock_pvp, mock_device):
        """PVP + gather with pending rallies: PVP dispatches, then waits (no gather)."""
        _quest_rallies_pending[(mock_device, QuestType.EVIL_GUARD)] = 1

        mock_ocr.return_value = [
            {"quest_type": QuestType.PVP, "current": 0, "target": 1, "completed": False,
             "text": "PvP(0/1)"},
            {"quest_type": QuestType.GATHER, "current": 0, "target": 1000000, "completed": False,
             "text": "Gather(0/200,000)"},
        ]

        check_quests(mock_device)
        # PVP should attempt attack
        mock_pvp.assert_called_once()
        # Gather should NOT run — pending rallies block it
        mock_gather.assert_not_called()
        # Should wait for pending rallies instead
        mock_wait.assert_called_once()


# ============================================================
# _all_quests_visually_complete: PVP cooldown awareness
# ============================================================

class TestAllQuestsVisuallyCompletePVP:
    def test_pvp_on_cooldown_is_ok(self, mock_device):
        """PVP quest incomplete but troop recently dispatched — don't block gold."""
        _pvp_last_dispatch[mock_device] = time.time() - 60  # 1 min ago
        quests = [
            {"quest_type": QuestType.PVP, "current": 0, "target": 1, "completed": False},
            {"quest_type": QuestType.TITAN, "current": 15, "target": 15, "completed": False},
        ]
        assert _all_quests_visually_complete(mock_device, quests) is True

    def test_pvp_no_cooldown_blocks(self, mock_device):
        """PVP quest incomplete and no recent dispatch — blocks gold mining."""
        quests = [
            {"quest_type": QuestType.PVP, "current": 0, "target": 1, "completed": False},
            {"quest_type": QuestType.TITAN, "current": 15, "target": 15, "completed": False},
        ]
        assert _all_quests_visually_complete(mock_device, quests) is False

    def test_pvp_cooldown_expired_blocks(self, mock_device):
        """PVP cooldown expired — quest blocks again."""
        _pvp_last_dispatch[mock_device] = time.time() - (_PVP_COOLDOWN_S + 10)
        quests = [
            {"quest_type": QuestType.PVP, "current": 0, "target": 1, "completed": False},
        ]
        assert _all_quests_visually_complete(mock_device, quests) is False


# ============================================================
# _attack_pvp_tower: handler unit tests
# ============================================================

class TestAttackPvpTower:
    @patch("actions.quests.tap_image", return_value=True)
    @patch("actions.quests.tap_tower_until_attack_menu", return_value=True)
    @patch("actions.quests.save_failure_screenshot")
    @patch("actions.quests.config")
    @patch("actions.quests.troops_avail", return_value=3)
    def test_success_full_flow(self, mock_troops, mock_config, mock_save,
                                mock_tap_tower, mock_tap_image, mock_device):
        """Happy path: target succeeds, attack menu opens, depart found."""
        mock_config.set_device_status = MagicMock()
        with patch("actions.combat.target", return_value=True), \
             patch("actions.quests.time.sleep"):
            result = _attack_pvp_tower(mock_device)
            assert result is True
            assert mock_device in _pvp_last_dispatch
            # Verify cooldown timestamp is recent
            assert time.time() - _pvp_last_dispatch[mock_device] < 5

    def test_cooldown_skips(self, mock_device):
        """Recent dispatch within cooldown — should skip without calling target."""
        _pvp_last_dispatch[mock_device] = time.time() - 60  # 1 min ago
        with patch("actions.combat.target") as mock_target:
            result = _attack_pvp_tower(mock_device)
            assert result is False
            mock_target.assert_not_called()

    @patch("actions.quests.config")
    @patch("actions.quests.troops_avail", return_value=0)
    def test_no_troops_skips(self, mock_troops, mock_config, mock_device):
        """Zero troops available — skip without calling target."""
        mock_config.set_device_status = MagicMock()
        with patch("actions.combat.target") as mock_target:
            result = _attack_pvp_tower(mock_device)
            assert result is False
            mock_target.assert_not_called()

    @patch("actions.quests.save_failure_screenshot")
    @patch("actions.quests.config")
    @patch("actions.quests.troops_avail", return_value=3)
    def test_target_fails(self, mock_troops, mock_config, mock_save, mock_device):
        """target() returns False — should save screenshot and return False."""
        mock_config.set_device_status = MagicMock()
        with patch("actions.combat.target", return_value=False):
            result = _attack_pvp_tower(mock_device)
            assert result is False
            mock_save.assert_called_once_with(mock_device, "pvp_target_fail")

    @patch("actions.quests.save_failure_screenshot")
    @patch("actions.quests.config")
    @patch("actions.quests.troops_avail", return_value=3)
    def test_target_no_marker(self, mock_troops, mock_config, mock_save, mock_device):
        """target() returns 'no_marker' (truthy!) — should still fail."""
        mock_config.set_device_status = MagicMock()
        with patch("actions.combat.target", return_value="no_marker"):
            result = _attack_pvp_tower(mock_device)
            assert result is False
            mock_save.assert_called_once_with(mock_device, "pvp_target_fail")

    @patch("actions.quests.tap_tower_until_attack_menu", return_value=False)
    @patch("actions.quests.save_failure_screenshot")
    @patch("actions.quests.config")
    @patch("actions.quests.troops_avail", return_value=3)
    def test_attack_menu_miss(self, mock_troops, mock_config, mock_save,
                               mock_tap_tower, mock_device):
        """Attack menu doesn't open — save screenshot."""
        mock_config.set_device_status = MagicMock()
        with patch("actions.combat.target", return_value=True):
            result = _attack_pvp_tower(mock_device)
            assert result is False
            mock_save.assert_called_once_with(mock_device, "pvp_attack_menu_fail")

    @patch("actions.quests.tap_image", return_value=False)
    @patch("actions.quests.tap_tower_until_attack_menu", return_value=True)
    @patch("actions.quests.save_failure_screenshot")
    @patch("actions.quests.config")
    @patch("actions.quests.troops_avail", return_value=3)
    def test_depart_miss(self, mock_troops, mock_config, mock_save,
                          mock_tap_tower, mock_tap_image, mock_device):
        """Depart button not found — save screenshot."""
        mock_config.set_device_status = MagicMock()
        with patch("actions.combat.target", return_value=True):
            result = _attack_pvp_tower(mock_device)
            assert result is False
            mock_save.assert_called_once_with(mock_device, "pvp_depart_fail")

    @patch("actions.quests.tap_image", return_value=True)
    @patch("actions.quests.tap_tower_until_attack_menu", return_value=True)
    @patch("actions.quests.save_failure_screenshot")
    @patch("actions.quests.config")
    @patch("actions.quests.troops_avail", return_value=3)
    def test_respects_stop_check_after_target(self, mock_troops, mock_config,
                                               mock_save, mock_tap_tower,
                                               mock_tap_image, mock_device):
        """Stop check fires after target() — should abort before attack menu."""
        mock_config.set_device_status = MagicMock()
        stop = MagicMock(return_value=True)  # stop immediately
        with patch("actions.combat.target", return_value=True):
            result = _attack_pvp_tower(mock_device, stop_check=stop)
            assert result is False
            mock_tap_tower.assert_not_called()


# ============================================================
# check_quests: PVP dispatch integration
# ============================================================

class TestPvpDispatchIntegration:
    @patch("actions.quests._attack_pvp_tower")
    @patch("actions.quests._run_tower_quest")
    @patch("actions.farming.gather_gold_loop")
    @patch("actions.quests.navigate", return_value=True)
    @patch("actions.quests._claim_quest_rewards", return_value=0)
    @patch("actions.quests._ocr_quest_rows")
    def test_pvp_dispatches_when_actionable(self, mock_ocr, mock_claim,
                                              mock_nav, mock_gather,
                                              mock_tower, mock_pvp,
                                              mock_device):
        """PVP quest triggers _attack_pvp_tower in dispatch."""
        mock_ocr.return_value = [
            {"quest_type": QuestType.PVP, "current": 0, "target": 1, "completed": False,
             "text": "PvP(0/1)"},
            {"quest_type": QuestType.GATHER, "current": 0, "target": 1000000, "completed": False,
             "text": "Gather(0/200,000)"},
        ]

        check_quests(mock_device)
        mock_pvp.assert_called_once()
        # Gather should still run (no pending rallies)
        mock_gather.assert_called_once()

    @patch("actions.quests._attack_pvp_tower")
    @patch("actions.quests._run_rally_loop", return_value=False)
    @patch("actions.quests._run_tower_quest")
    @patch("actions.farming.gather_gold_loop")
    @patch("actions.quests.navigate", return_value=True)
    @patch("actions.quests._claim_quest_rewards", return_value=0)
    @patch("actions.quests._ocr_quest_rows")
    def test_pvp_runs_before_rallies(self, mock_ocr, mock_claim, mock_nav,
                                       mock_gather, mock_tower,
                                       mock_rally_loop, mock_pvp,
                                       mock_device):
        """PVP runs before rally loop when both PVP and titan quests present."""
        mock_ocr.return_value = [
            {"quest_type": QuestType.PVP, "current": 0, "target": 1, "completed": False,
             "text": "PvP(0/1)"},
            {"quest_type": QuestType.TITAN, "current": 0, "target": 15, "completed": False,
             "text": "Defeat Titans(0/15)"},
        ]

        check_quests(mock_device)
        mock_pvp.assert_called_once()
        mock_rally_loop.assert_called_once()

    @patch("actions.quests._attack_pvp_tower")
    @patch("actions.quests._run_tower_quest")
    @patch("actions.farming.gather_gold_loop")
    @patch("actions.quests.navigate", return_value=True)
    @patch("actions.quests._claim_quest_rewards", return_value=0)
    @patch("actions.quests._ocr_quest_rows")
    def test_pvp_on_cooldown_still_gathers(self, mock_ocr, mock_claim,
                                              mock_nav, mock_gather,
                                              mock_tower, mock_pvp,
                                              mock_device):
        """PVP on cooldown falls through to gather."""
        mock_pvp.return_value = False  # cooldown skip
        mock_ocr.return_value = [
            {"quest_type": QuestType.PVP, "current": 0, "target": 1, "completed": False,
             "text": "PvP(0/1)"},
            {"quest_type": QuestType.GATHER, "current": 0, "target": 1000000, "completed": False,
             "text": "Gather(0/200,000)"},
        ]

        check_quests(mock_device)
        mock_pvp.assert_called_once()
        mock_gather.assert_called_once()
