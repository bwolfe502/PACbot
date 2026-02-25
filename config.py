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

# Mithril mining
MITHRIL_ENABLED = False
MITHRIL_INTERVAL = 19        # minutes between refresh cycles
LAST_MITHRIL_TIME = {}       # {device_id: timestamp} — last mine_mithril run
MITHRIL_DEPLOY_TIME = {}     # {device_id: timestamp} — when troops were deployed to mines

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

MY_TEAM_COLOR = "yellow"
ENEMY_TEAMS = ["green"]
running_tasks = {}
auto_occupy_running = False
auto_occupy_thread = None

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
    "green":  (115, 219, 132),
    "red":    (49, 85, 247),
    "blue":   (214, 154, 132)
}

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

def set_territory_config(my_team, enemy_teams):
    """Set which team you are and which teams to attack"""
    global MY_TEAM_COLOR, ENEMY_TEAMS
    MY_TEAM_COLOR = my_team
    ENEMY_TEAMS = enemy_teams
    _log.info("Territory config: My team = %s, Attacking = %s", my_team, enemy_teams)
