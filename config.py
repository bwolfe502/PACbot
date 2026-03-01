import platform
import shutil
import os
import glob
from enum import Enum

# ============================================================
# ENUMS — quest types, rally types, screen names
# ============================================================

class _StrEnum(str, Enum):
    """str-compatible enum — works in ==, dict keys, and f-strings."""
    def __format__(self, format_spec):
        return str.__format__(self.value, format_spec)


class QuestType(_StrEnum):
    TITAN = "titan"
    EVIL_GUARD = "eg"
    PVP = "pvp"
    GATHER = "gather"
    FORTRESS = "fortress"
    TOWER = "tower"


class RallyType(_StrEnum):
    CASTLE = "castle"
    PASS = "pass"
    TOWER = "tower"
    GROOT = "groot"


class Screen(_StrEnum):
    MAP = "map_screen"
    BATTLE_LIST = "bl_screen"
    ALLIANCE_QUEST = "aq_screen"
    TROOP_DETAIL = "td_screen"
    TERRITORY = "territory_screen"
    WAR = "war_screen"
    PROFILE = "profile_screen"
    ALLIANCE = "alliance_screen"
    KINGDOM = "kingdom_screen"
    UNKNOWN = "unknown"
    LOGGED_OUT = "logged_out"

# ============================================================
# ADB PATH - auto-detect per platform
# ============================================================

def _find_adb():
    """Find the ADB executable for the current platform."""
    system = platform.system()

    # Check for platform-tools bundled in the project directory first
    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_adb = os.path.join(script_dir, "platform-tools", "adb" + (".exe" if system == "Windows" else ""))
    if os.path.isfile(local_adb):
        return local_adb

    if system == "Windows":
        # BlueStacks bundled ADB
        bluestacks_adb = r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe"
        if os.path.isfile(bluestacks_adb):
            return bluestacks_adb

        # MuMu Player 12 bundled ADB
        mumu_pattern = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                                    "Netease", "MuMuPlayer-12.0", "shell", "adb.exe")
        if os.path.isfile(mumu_pattern):
            return mumu_pattern

        # MuMu Player (older) — check common install paths
        for drive in ["C", "D"]:
            for path in glob.glob(f"{drive}:\\MuMu*\\shell\\adb.exe"):
                if os.path.isfile(path):
                    return path

        # Android SDK adb on PATH
        found = shutil.which("adb")
        if found:
            return found

        # Common Windows SDK location
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            sdk_adb = os.path.join(local, "Android", "Sdk", "platform-tools", "adb.exe")
            if os.path.isfile(sdk_adb):
                return sdk_adb

    else:
        # macOS / Linux — check PATH first
        found = shutil.which("adb")
        if found:
            return found

        # Common macOS SDK location
        home = os.path.expanduser("~")
        for candidate in [
            os.path.join(home, "Library", "Android", "sdk", "platform-tools", "adb"),
            "/usr/local/bin/adb",
            "/opt/homebrew/bin/adb",
        ]:
            if os.path.isfile(candidate):
                return candidate

        # BlueStacks Mac — bundled adb
        bluestacks_mac = "/Applications/BlueStacks.app/Contents/MacOS/hd-adb"
        if os.path.isfile(bluestacks_mac):
            return bluestacks_mac

        # MuMu Player Mac — bundled adb
        mumu_mac = "/Applications/MuMuPlayer.app/Contents/MacOS/tools/adb"
        if os.path.isfile(mumu_mac):
            return mumu_mac

    # Last resort
    import logging
    logging.getLogger("config").warning("Could not find ADB. Install Android SDK platform-tools and make sure 'adb' is on your PATH.")
    return "adb"  # Hope it's on PATH

adb_path = _find_adb()
# Log after setup_logging is called; print for immediate visibility during import
print(f"Using ADB: {adb_path}")

def log_adb_path():
    """Log the ADB path after logging is initialized. Call from main."""
    from botlog import get_logger
    get_logger("config").info("ADB path: %s", adb_path)

# ============================================================
# KNOWN EMULATOR ADB PORTS (for auto-connect)
# ============================================================

# MuMu Player 12: base port 16384, +32 per instance (up to 8 instances)
# MuMu Player 5:  base port 5555, +2 per instance
# BlueStacks 5:   base port 5555, +10 per instance (5555, 5565, 5575, 5585)
EMULATOR_PORTS = {
    "MuMu12":     [16384 + (i * 32) for i in range(8)],
    "MuMu5":      [5555 + (i * 2)   for i in range(8)],
    "BlueStacks": [5555 + (i * 10)  for i in range(10)],
}

# ============================================================
# GLOBAL STATE
# ============================================================

DEVICE_TOTAL_TROOPS = {}   # {device_id: int} - total troops per account (default 5)
LAST_ATTACKED_SQUARE = {}  # {device_id: (row, col)} - per device tracking
MANUAL_ATTACK_SQUARES = set()  # Squares user manually selected to attack: {(row, col), ...}
MANUAL_IGNORE_SQUARES = set()  # Squares user manually selected to ignore: {(row, col), ...}
MIN_TROOPS_AVAILABLE = 0
AUTO_HEAL_ENABLED = False
AUTO_RESTORE_AP_ENABLED = False
EG_RALLY_OWN_ENABLED = True    # If False, only join EG rallies — never start own
TITAN_RALLY_OWN_ENABLED = True # If False, only join Titan rallies — never start own

# AP restore source options
AP_USE_FREE = True           # Use free AP restores (25 AP each, 2x daily)
AP_USE_POTIONS = True        # Use AP potions
AP_ALLOW_LARGE_POTIONS = True  # Allow 100 & 200 AP potions (vs only 10/20/50)
AP_USE_GEMS = False          # Use gems to restore AP (50 AP per use)
AP_GEM_LIMIT = 0             # Max gems to spend per restore session (0 = disabled, max 3500)

# AP costs for PvE actions
AP_COST_RALLY_TITAN = 20
AP_COST_EVIL_GUARD = 70
CLICK_TRAIL_ENABLED = True

# ADB & vision constants
ADB_COMMAND_TIMEOUT = 10         # seconds — timeout for adb tap/swipe/screenshot
SCREEN_MATCH_THRESHOLD = 0.8    # confidence required to identify a screen

# Debug screenshot limits (rolling cleanup)
DEBUG_SCREENSHOT_MAX = 50        # max debug screenshots before cleanup
CLICK_TRAIL_MAX = 50             # max click trail images before cleanup
FAILURE_SCREENSHOT_MAX = 200     # max failure screenshots (persistent)

# Safety caps for action loops
MAX_RALLY_ATTEMPTS = 15          # max iterations in rally join loop
MAX_HEAL_ITERATIONS = 20         # max heal_all cycles (5 troops + safety buffer)
QUEST_PENDING_TIMEOUT = 360      # seconds — timeout for quest-pending rally (6 min)
RALLY_PANEL_WAIT_ENABLED = True  # Use troop panel to wait for rallies (vs counter polling)
RALLY_WAIT_POLL_INTERVAL = 5     # seconds between panel status polls while waiting

# Mithril mining
MITHRIL_ENABLED = False
MITHRIL_INTERVAL = 19        # minutes between refresh cycles
LAST_MITHRIL_TIME = {}       # {device_id: timestamp} — last mine_mithril run
MITHRIL_DEPLOY_TIME = {}     # {device_id: timestamp} — when troops were deployed to mines

# Gather gold
GATHER_ENABLED = True
GATHER_MINE_LEVEL = 4            # Gold mine level to search for (4, 5, or 6)
GATHER_MAX_TROOPS = 3            # Max troops to send gathering simultaneously

# Tower quest
TOWER_QUEST_ENABLED = False      # Occupy tower for alliance quest (requires target marker on tower)

# Per-device lock — prevents concurrent tasks from controlling the same device
import threading
_device_locks = {}
_device_locks_guard = threading.Lock()

def get_device_lock(device):
    """Return a Lock for the given device, creating it on first use."""
    with _device_locks_guard:
        if device not in _device_locks:
            _device_locks[device] = threading.Lock()
        return _device_locks[device]

ALL_TEAMS = ["yellow", "green", "red", "blue"]
MY_TEAM_COLOR = "red"
ENEMY_TEAMS = ["yellow", "green", "blue"]
running_tasks = {}
DEVICE_STATUS = {}   # {device_id: "status message"} — shown in GUI

def set_device_status(device, msg):
    """Set the current status message for a device (shown in GUI)."""
    DEVICE_STATUS[device] = msg

def clear_device_status(device):
    """Clear status for a device (e.g. when task stops)."""
    DEVICE_STATUS.pop(device, None)

auto_occupy_running = False
auto_occupy_thread = None

# Thread-safe queue for alerts from task runners to GUI
import queue
alert_queue = queue.Queue()

BUTTONS = {
    "quest_button": {"x": 300, "y": 1100},
    "search_button": {"x": 900, "y": 1800},
}

# Territory grid constants
SQUARE_SIZE = 42.5
GRID_OFFSET_X = 31
GRID_OFFSET_Y = 147
GRID_WIDTH = 24
GRID_HEIGHT = 24
THRONE_SQUARES = {(11, 11), (11, 12), (12, 11), (12, 12)}

BORDER_COLORS = {
    "yellow": (107, 223, 239),
    "green":  (100, 175, 160),  # recalibrated from live diagnostic data 2026-02-28
    "red":    (49, 85, 247),
    "blue":   (148, 145, 165)  # recalibrated from live diagnostic data 2026-02-28
}

# ============================================================
# SETTINGS VALIDATION
# ============================================================

SETTINGS_RULES = {
    # Booleans — type check only
    "auto_heal":             {"type": bool},
    "auto_restore_ap":       {"type": bool},
    "ap_use_free":           {"type": bool},
    "ap_use_potions":        {"type": bool},
    "ap_allow_large_potions":{"type": bool},
    "ap_use_gems":           {"type": bool},
    "verbose_logging":       {"type": bool},
    "eg_rally_own":          {"type": bool},
    "titan_rally_own":       {"type": bool},
    "web_dashboard":         {"type": bool},
    "gather_enabled":        {"type": bool},
    "tower_quest_enabled":   {"type": bool},
    "remote_access":         {"type": bool},
    # Ints — type + optional min/max
    "ap_gem_limit":          {"type": int, "min": 0, "max": 3500},
    "min_troops":            {"type": int, "min": 0, "max": 5},
    "variation":             {"type": int, "min": 0},
    "titan_interval":        {"type": int, "min": 1},
    "groot_interval":        {"type": int, "min": 1},
    "reinforce_interval":    {"type": int, "min": 1},
    "pass_interval":         {"type": int, "min": 1},
    "mithril_interval":      {"type": int, "min": 1},
    "gather_mine_level":     {"type": int, "min": 4, "max": 6},
    "gather_max_troops":     {"type": int, "min": 1, "max": 5},
    # Strings — type + allowed values
    "pass_mode":             {"type": str, "choices": ["Rally Joiner", "Rally Starter"]},
    "my_team":               {"type": str, "choices": ["yellow", "red", "blue", "green"]},
    "enemy_team":            {"type": str, "choices": ["yellow", "red", "blue", "green"]},  # legacy — ignored, enemies auto-derived from my_team
    "mode":                  {"type": str, "choices": ["bl", "rw"]},
}


def validate_settings(settings, defaults):
    """Validate and sanitize a settings dict.

    Returns (cleaned_settings, warnings) where cleaned_settings is a new dict
    with invalid values replaced by defaults, and warnings is a list of strings
    describing what was fixed.
    """
    cleaned = dict(settings)
    warnings = []

    for key, rule in SETTINGS_RULES.items():
        if key not in cleaned:
            continue

        value = cleaned[key]
        expected = rule["type"]

        # Type check
        if expected is bool:
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value in (0, 1):
                cleaned[key] = bool(value)
                continue
            warnings.append(f"{key}: expected bool, got {type(value).__name__} — reset to default")
            cleaned[key] = defaults[key]
        elif expected is int:
            if isinstance(value, bool) or not isinstance(value, int):
                warnings.append(f"{key}: expected int, got {type(value).__name__} — reset to default")
                cleaned[key] = defaults[key]
                continue
            lo = rule.get("min")
            hi = rule.get("max")
            if lo is not None and value < lo:
                warnings.append(f"{key}: {value} below minimum {lo} — reset to default")
                cleaned[key] = defaults[key]
            elif hi is not None and value > hi:
                warnings.append(f"{key}: {value} above maximum {hi} — reset to default")
                cleaned[key] = defaults[key]
        elif expected is str:
            if not isinstance(value, str):
                warnings.append(f"{key}: expected str, got {type(value).__name__} — reset to default")
                cleaned[key] = defaults[key]
            elif "choices" in rule and value not in rule["choices"]:
                warnings.append(f"{key}: '{value}' not in {rule['choices']} — reset to default")
                cleaned[key] = defaults[key]

    # Special case: device_troops (nested dict, not in DEFAULTS)
    dt = cleaned.get("device_troops")
    if dt is not None:
        if not isinstance(dt, dict):
            warnings.append(f"device_troops: expected dict, got {type(dt).__name__} — removed")
            cleaned["device_troops"] = {}
        else:
            clean_dt = {}
            for dev_id, count in dt.items():
                if not isinstance(count, int) or isinstance(count, bool):
                    warnings.append(f"device_troops[{dev_id}]: expected int, got {type(count).__name__} — skipped")
                    continue
                if count < 1 or count > 5:
                    warnings.append(f"device_troops[{dev_id}]: {count} out of range [1, 5] — skipped")
                    continue
                clean_dt[dev_id] = count
            cleaned["device_troops"] = clean_dt

    return cleaned, warnings


# ============================================================
# CONFIGURATION SETTERS
# ============================================================

from botlog import get_logger as _get_logger
_log = _get_logger("config")

def set_min_troops(value):
    """Set the minimum troops available threshold"""
    global MIN_TROOPS_AVAILABLE
    MIN_TROOPS_AVAILABLE = value
    _log.info("Minimum troops available set to: %d", value)

def set_auto_heal(enabled):
    """Set the auto heal enabled state"""
    global AUTO_HEAL_ENABLED
    AUTO_HEAL_ENABLED = enabled
    _log.info("Auto heal: %s", "enabled" if enabled else "disabled")

def set_auto_restore_ap(enabled):
    """Set the auto restore AP enabled state"""
    global AUTO_RESTORE_AP_ENABLED
    AUTO_RESTORE_AP_ENABLED = enabled
    _log.info("Auto restore AP: %s", "enabled" if enabled else "disabled")

def set_ap_restore_options(use_free, use_potions, allow_large, use_gems, gem_limit):
    """Set AP restore source preferences"""
    global AP_USE_FREE, AP_USE_POTIONS, AP_ALLOW_LARGE_POTIONS, AP_USE_GEMS, AP_GEM_LIMIT
    AP_USE_FREE = use_free
    AP_USE_POTIONS = use_potions
    AP_ALLOW_LARGE_POTIONS = allow_large
    AP_USE_GEMS = use_gems
    AP_GEM_LIMIT = max(0, min(gem_limit, 3500))
    _log.info("AP restore: free=%s, potions=%s, large=%s, gems=%s, gem_limit=%d",
              use_free, use_potions, allow_large, use_gems, AP_GEM_LIMIT)

def set_eg_rally_own(enabled):
    """Set whether the bot can start its own EG rallies (vs join-only)"""
    global EG_RALLY_OWN_ENABLED
    EG_RALLY_OWN_ENABLED = enabled
    _log.info("EG rally own: %s", "enabled" if enabled else "disabled")

def set_titan_rally_own(enabled):
    """Set whether the bot can start its own Titan rallies (vs join-only)"""
    global TITAN_RALLY_OWN_ENABLED
    TITAN_RALLY_OWN_ENABLED = enabled
    _log.info("Titan rally own: %s", "enabled" if enabled else "disabled")

def set_tower_quest_enabled(enabled):
    """Set the tower quest enabled state."""
    global TOWER_QUEST_ENABLED
    TOWER_QUEST_ENABLED = enabled
    _log.info("Tower quest: %s", "enabled" if enabled else "disabled")

def set_gather_options(enabled, mine_level, max_troops):
    """Set gather gold preferences."""
    global GATHER_ENABLED, GATHER_MINE_LEVEL, GATHER_MAX_TROOPS
    GATHER_ENABLED = enabled
    GATHER_MINE_LEVEL = max(4, min(mine_level, 6))
    GATHER_MAX_TROOPS = max(1, min(max_troops, 5))
    _log.info("Gather config: enabled=%s, mine_level=%d, max_troops=%d",
              GATHER_ENABLED, GATHER_MINE_LEVEL, GATHER_MAX_TROOPS)

def set_territory_config(my_team):
    """Set which team you are; all other teams become enemies automatically."""
    global MY_TEAM_COLOR, ENEMY_TEAMS
    MY_TEAM_COLOR = my_team
    ENEMY_TEAMS = [t for t in ALL_TEAMS if t != my_team]
    _log.info("Territory config: My team = %s, Enemies = %s", my_team, ENEMY_TEAMS)
