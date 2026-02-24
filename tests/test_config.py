"""Tests for config setters (config.py)."""

import config
from config import (
    set_min_troops, set_auto_heal, set_auto_restore_ap,
    set_ap_restore_options, set_eg_rally_own, set_territory_config,
)


class TestSetMinTroops:
    def test_sets_value(self):
        set_min_troops(3)
        assert config.MIN_TROOPS_AVAILABLE == 3

    def test_sets_zero(self):
        set_min_troops(0)
        assert config.MIN_TROOPS_AVAILABLE == 0


class TestSetAutoHeal:
    def test_enable(self):
        set_auto_heal(True)
        assert config.AUTO_HEAL_ENABLED is True

    def test_disable(self):
        set_auto_heal(False)
        assert config.AUTO_HEAL_ENABLED is False


class TestSetAutoRestoreAp:
    def test_enable(self):
        set_auto_restore_ap(True)
        assert config.AUTO_RESTORE_AP_ENABLED is True

    def test_disable(self):
        set_auto_restore_ap(False)
        assert config.AUTO_RESTORE_AP_ENABLED is False


class TestSetApRestoreOptions:
    def test_all_enabled(self):
        set_ap_restore_options(True, True, True, True, 1000)
        assert config.AP_USE_FREE is True
        assert config.AP_USE_POTIONS is True
        assert config.AP_ALLOW_LARGE_POTIONS is True
        assert config.AP_USE_GEMS is True
        assert config.AP_GEM_LIMIT == 1000

    def test_gem_limit_clamped_high(self):
        set_ap_restore_options(True, True, True, True, 9999)
        assert config.AP_GEM_LIMIT == 3500

    def test_gem_limit_clamped_negative(self):
        set_ap_restore_options(True, True, True, True, -5)
        assert config.AP_GEM_LIMIT == 0

    def test_gem_limit_zero(self):
        set_ap_restore_options(False, False, False, False, 0)
        assert config.AP_GEM_LIMIT == 0
        assert config.AP_USE_FREE is False
        assert config.AP_USE_GEMS is False


class TestSetEgRallyOwn:
    def test_enable(self):
        set_eg_rally_own(True)
        assert config.EG_RALLY_OWN_ENABLED is True

    def test_disable(self):
        set_eg_rally_own(False)
        assert config.EG_RALLY_OWN_ENABLED is False


class TestSetTerritoryConfig:
    def test_sets_teams(self):
        set_territory_config("blue", ["red", "green"])
        assert config.MY_TEAM_COLOR == "blue"
        assert config.ENEMY_TEAMS == ["red", "green"]

    def test_single_enemy(self):
        set_territory_config("yellow", ["green"])
        assert config.MY_TEAM_COLOR == "yellow"
        assert config.ENEMY_TEAMS == ["green"]
