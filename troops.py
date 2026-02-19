import numpy as np
import time

from vision import load_screenshot, tap_image, adb_tap
from navigation import navigate



# ============================================================
# TROOP MANAGEMENT
# ============================================================

# Pixel positions and patterns for troop detection
_TROOP_X = 160
_TROOP_COLOR = np.array([107, 247, 255])
_TROOP_TOLERANCE = 10

# Y positions for each troop count pattern
# 0 troops: all 5 slots occupied at these Y positions
# 1 troop: 4 slots at shifted positions, etc.
_SLOT_PATTERNS = {
    0: {"match": [640, 800, 960, 1110, 1270], "no_match": []},
    1: {"match": [720, 880, 1040, 1200], "no_match": []},
    2: {"match": [800, 960, 1110], "no_match": [640, 1270]},
    3: {"match": [880, 1040], "no_match": [720, 1200]},
    4: {"match": [960], "no_match": [640, 800, 1110, 1270]},
}

def troops_avail(device):
    """Check how many troops are available (0-5) by checking pixel colors"""
    screen = load_screenshot(device)

    if screen is None:
        print(f"[{device}] Failed to load screenshot for troops check")
        return 5

    def is_yellow(y):
        pixel = screen[y, _TROOP_X].astype(np.int16)
        return np.all(np.abs(pixel - _TROOP_COLOR) < _TROOP_TOLERANCE)

    for count, pattern in _SLOT_PATTERNS.items():
        if (all(is_yellow(y) for y in pattern["match"]) and
            all(not is_yellow(y) for y in pattern["no_match"])):
            print(f"[{device}] Troops available: {count}")
            return count

    print(f"[{device}] Troops available: 5")
    return 5

def all_troops_home(device):
    """Check if all troops are home (troops_avail returns 5)"""
    return troops_avail(device) == 5

def heal_all(device):
    """Check if we're on map_screen and if heal is needed, then heal all troops.
    Repeats until no more heal button is found (all troops healed)."""
    if not navigate("map_screen", device):
        print(f"[{device}] Failed to navigate to map screen for healing")
        return

    healed_any = False
    while tap_image("heal.png", device):
        healed_any = True
        print(f"[{device}] Starting heal sequence...")
        time.sleep(1)
        adb_tap(device, 700, 1460)
        time.sleep(1)
        adb_tap(device, 542, 1425)
        time.sleep(1)
        adb_tap(device, 1000, 200)
        time.sleep(2)

    if healed_any:
        print(f"[{device}] Heal sequence complete — all troops healed")
        # Navigate back to map_screen to ensure clean state
        navigate("map_screen", device)
    else:
        print(f"[{device}] No healing needed")
