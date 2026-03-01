"""Shared settings persistence for PACbot.

Provides load/save for settings.json with validation and defaults.
Used by both main.py (GUI) and web/dashboard.py (Flask).

Key exports:
    SETTINGS_FILE — absolute path to settings.json
    DEFAULTS      — default settings dict (23 keys)
    load_settings — load + validate + merge with defaults
    save_settings — write settings dict to JSON
"""

import json
import os

from botlog import get_logger
from config import validate_settings

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

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
    "my_team": "red",
    "mode": "bl",
    "verbose_logging": False,
    "eg_rally_own": True,
    "titan_rally_own": True,
    "mithril_interval": 19,
    "web_dashboard": False,
    "gather_enabled": True,
    "gather_mine_level": 4,
    "gather_max_troops": 3,
    "tower_quest_enabled": False,
    "relay_enabled": False,
    "relay_url": "",
    "relay_secret": "",
    "relay_bot_name": "",
}


def load_settings():
    """Load settings from disk, merging with defaults and validating."""
    _log = get_logger("settings")
    try:
        with open(SETTINGS_FILE, "r") as f:
            saved = json.load(f)
        merged = {**DEFAULTS, **saved}
        merged, warnings = validate_settings(merged, DEFAULTS)
        for w in warnings:
            _log.warning("Settings: %s", w)
        _log.info("Settings loaded (%d keys, %d from file)", len(merged), len(saved))
        return merged
    except FileNotFoundError:
        _log.info("No settings file found, using defaults (%d keys)", len(DEFAULTS))
        return dict(DEFAULTS)
    except json.JSONDecodeError as e:
        _log.warning("Settings file corrupted (%s), using defaults", e)
        return dict(DEFAULTS)


def save_settings(settings):
    """Write settings dict to settings.json."""
    _log = get_logger("settings")
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
        _log.debug("Settings saved (%d keys)", len(settings))
    except Exception as e:
        _log.error("Failed to save settings: %s", e)
