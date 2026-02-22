import platform
import shutil
import os
import glob

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
    print("WARNING: Could not find ADB. Install Android SDK platform-tools and make sure 'adb' is on your PATH.")
    return "adb"  # Hope it's on PATH

adb_path = _find_adb()
print(f"Using ADB: {adb_path}")


# ============================================================
# GLOBAL STATE
# ============================================================

LAST_ATTACKED_SQUARE = {}  # {device_id: (row, col)} - per device tracking
MANUAL_ATTACK_SQUARES = set()  # Squares user manually selected to attack: {(row, col), ...}
MANUAL_IGNORE_SQUARES = set()  # Squares user manually selected to ignore: {(row, col), ...}
MIN_TROOPS_AVAILABLE = 0
AUTO_HEAL_ENABLED = False
CLICK_TRAIL_ENABLED = True
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

def set_min_troops(value):
    """Set the minimum troops available threshold"""
    global MIN_TROOPS_AVAILABLE
    MIN_TROOPS_AVAILABLE = value
    print(f"Minimum troops available set to: {value}")

def set_auto_heal(enabled):
    """Set the auto heal enabled state"""
    global AUTO_HEAL_ENABLED
    AUTO_HEAL_ENABLED = enabled
    print(f"Auto heal: {'enabled' if enabled else 'disabled'}")

def set_territory_config(my_team, enemy_teams):
    """Set which team you are and which teams to attack"""
    global MY_TEAM_COLOR, ENEMY_TEAMS
    MY_TEAM_COLOR = my_team
    ENEMY_TEAMS = enemy_teams
    print(f"Territory config: My team = {my_team}, Attacking = {enemy_teams}")
