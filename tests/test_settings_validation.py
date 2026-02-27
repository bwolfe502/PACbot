"""Tests for settings validation (config.validate_settings)."""

import pytest
from config import validate_settings, SETTINGS_RULES


# Mirror of main.DEFAULTS — kept here so tests don't depend on main.py / tkinter
DEFAULTS = {
    "auto_heal": True,
    "auto_restore_ap": False,
    "ap_use_free": True,
    "ap_use_potions": True,
    "ap_allow_large_potions": True,
    "ap_use_gems": False,
    "ap_gem_limit": 0,
    "min_troops": 0,
    "variation": 0,
    "titan_interval": 30,
    "groot_interval": 30,
    "reinforce_interval": 30,
    "pass_interval": 30,
    "pass_mode": "Rally Joiner",
    "my_team": "yellow",
    "enemy_team": "green",
    "mode": "bl",
    "verbose_logging": False,
    "eg_rally_own": True,
    "mithril_interval": 19,
    "web_dashboard": False,
}


class TestValidPassthrough:
    """Valid settings should pass through unchanged."""

    def test_valid_settings_unchanged(self):
        settings = dict(DEFAULTS)
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned == settings
        assert warnings == []

    def test_defaults_pass_validation(self):
        cleaned, warnings = validate_settings(DEFAULTS, DEFAULTS)
        assert warnings == []

    def test_unknown_keys_preserved(self):
        settings = {**DEFAULTS, "future_feature": 42, "beta_flag": "on"}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["future_feature"] == 42
        assert cleaned["beta_flag"] == "on"
        assert warnings == []


class TestBoolValidation:
    """Boolean setting validation."""

    def test_bool_correct_type(self):
        settings = {**DEFAULTS, "auto_heal": False}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["auto_heal"] is False
        assert warnings == []

    def test_bool_wrong_type_string(self):
        settings = {**DEFAULTS, "auto_heal": "yes"}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["auto_heal"] is True  # reset to default
        assert len(warnings) == 1
        assert "auto_heal" in warnings[0]

    def test_bool_int_zero_coerced(self):
        settings = {**DEFAULTS, "auto_heal": 0}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["auto_heal"] is False
        assert warnings == []

    def test_bool_int_one_coerced(self):
        settings = {**DEFAULTS, "auto_heal": 1}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["auto_heal"] is True
        assert warnings == []

    def test_bool_int_other_rejected(self):
        settings = {**DEFAULTS, "auto_heal": 5}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["auto_heal"] is True  # reset to default
        assert len(warnings) == 1

    def test_bool_none_rejected(self):
        settings = {**DEFAULTS, "verbose_logging": None}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["verbose_logging"] is False  # reset to default
        assert len(warnings) == 1


class TestIntRangeValidation:
    """Integer setting validation with range checks."""

    def test_int_in_range(self):
        settings = {**DEFAULTS, "ap_gem_limit": 1000}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["ap_gem_limit"] == 1000
        assert warnings == []

    @pytest.mark.parametrize("key,value", [
        ("ap_gem_limit", -1),
        ("min_troops", -1),
        ("titan_interval", 0),
        ("variation", -5),
    ])
    def test_int_below_min(self, key, value):
        settings = {**DEFAULTS, key: value}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned[key] == DEFAULTS[key]
        assert len(warnings) == 1
        assert key in warnings[0]

    @pytest.mark.parametrize("key,value", [
        ("ap_gem_limit", 5000),
        ("min_troops", 6),
    ])
    def test_int_above_max(self, key, value):
        settings = {**DEFAULTS, key: value}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned[key] == DEFAULTS[key]
        assert len(warnings) == 1
        assert key in warnings[0]

    def test_int_wrong_type_string(self):
        settings = {**DEFAULTS, "titan_interval": "thirty"}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["titan_interval"] == 30
        assert len(warnings) == 1

    def test_int_wrong_type_float(self):
        settings = {**DEFAULTS, "variation": 2.5}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["variation"] == 0
        assert len(warnings) == 1

    def test_int_bool_rejected(self):
        """bool is subclass of int — must be explicitly caught."""
        settings = {**DEFAULTS, "min_troops": True}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["min_troops"] == 0  # reset to default
        assert len(warnings) == 1

    @pytest.mark.parametrize("key,value", [
        ("ap_gem_limit", 0),
        ("ap_gem_limit", 3500),
        ("min_troops", 0),
        ("min_troops", 5),
        ("titan_interval", 1),
        ("variation", 0),
    ])
    def test_int_at_boundary(self, key, value):
        settings = {**DEFAULTS, key: value}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned[key] == value
        assert warnings == []

    def test_int_no_max_large_value(self):
        settings = {**DEFAULTS, "variation": 9999}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["variation"] == 9999
        assert warnings == []


class TestStringChoicesValidation:
    """String setting validation with allowed values."""

    @pytest.mark.parametrize("key,value", [
        ("mode", "rw"),
        ("pass_mode", "Rally Starter"),
        ("my_team", "red"),
        ("enemy_team", "blue"),
    ])
    def test_str_valid_choice(self, key, value):
        settings = {**DEFAULTS, key: value}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned[key] == value
        assert warnings == []

    def test_str_invalid_choice(self):
        settings = {**DEFAULTS, "mode": "pvp"}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["mode"] == "bl"  # reset to default
        assert len(warnings) == 1
        assert "mode" in warnings[0]

    def test_str_wrong_type_int(self):
        settings = {**DEFAULTS, "my_team": 1}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["my_team"] == "yellow"
        assert len(warnings) == 1

    def test_str_case_sensitive(self):
        settings = {**DEFAULTS, "pass_mode": "rally joiner"}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["pass_mode"] == "Rally Joiner"  # reset to default
        assert len(warnings) == 1


class TestDeviceTroops:
    """device_troops nested dict validation."""

    def test_valid_device_troops(self):
        settings = {**DEFAULTS, "device_troops": {"127.0.0.1:5555": 3}}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["device_troops"] == {"127.0.0.1:5555": 3}
        assert warnings == []

    def test_device_troops_not_dict(self):
        settings = {**DEFAULTS, "device_troops": "bad"}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["device_troops"] == {}
        assert len(warnings) == 1

    def test_device_troops_value_out_of_range_low(self):
        settings = {**DEFAULTS, "device_troops": {"dev": 0}}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["device_troops"] == {}
        assert len(warnings) == 1

    def test_device_troops_value_out_of_range_high(self):
        settings = {**DEFAULTS, "device_troops": {"dev": 6}}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["device_troops"] == {}
        assert len(warnings) == 1

    def test_device_troops_value_wrong_type(self):
        settings = {**DEFAULTS, "device_troops": {"dev": "three"}}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["device_troops"] == {}
        assert len(warnings) == 1

    def test_device_troops_bool_value_rejected(self):
        settings = {**DEFAULTS, "device_troops": {"dev": True}}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["device_troops"] == {}
        assert len(warnings) == 1

    def test_device_troops_absent(self):
        settings = dict(DEFAULTS)  # no device_troops key
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert "device_troops" not in cleaned
        assert warnings == []

    def test_device_troops_partial_valid(self):
        settings = {**DEFAULTS, "device_troops": {
            "good_dev": 3,
            "bad_dev": 0,
            "also_good": 5,
        }}
        cleaned, warnings = validate_settings(settings, DEFAULTS)
        assert cleaned["device_troops"] == {"good_dev": 3, "also_good": 5}
        assert len(warnings) == 1  # one warning for bad_dev


class TestWarnings:
    """Warning message quality and side effects."""

    def test_warning_contains_key_name(self):
        settings = {**DEFAULTS, "ap_gem_limit": "bad"}
        _, warnings = validate_settings(settings, DEFAULTS)
        assert "ap_gem_limit" in warnings[0]

    def test_multiple_warnings(self):
        settings = {**DEFAULTS, "auto_heal": "bad", "mode": "bad", "min_troops": -1}
        _, warnings = validate_settings(settings, DEFAULTS)
        assert len(warnings) == 3

    def test_no_mutation_of_input(self):
        settings = {**DEFAULTS, "ap_gem_limit": 9999, "auto_heal": "bad"}
        original = dict(settings)
        validate_settings(settings, DEFAULTS)
        assert settings == original


class TestRulesCompleteness:
    """Ensure SETTINGS_RULES and DEFAULTS stay in sync."""

    def test_every_default_has_rule(self):
        for key in DEFAULTS:
            assert key in SETTINGS_RULES, f"DEFAULTS key '{key}' missing from SETTINGS_RULES"

    def test_every_rule_has_default(self):
        for key in SETTINGS_RULES:
            assert key in DEFAULTS, f"SETTINGS_RULES key '{key}' missing from DEFAULTS"
