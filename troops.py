import cv2
import numpy as np
import time

import config
from vision import load_screenshot, tap_image, adb_tap, logged_tap, get_template
from navigation import navigate
from botlog import get_logger, timed_action



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
    """Check how many troops are available (0-5) by checking pixel colors.
    Only valid on map_screen — verifies using the screenshot before reading pixels."""
    log = get_logger("troops", device)
    screen = load_screenshot(device)

    if screen is None:
        log.warning("Failed to load screenshot for troops check")
        return 0

    # Verify we're on map_screen using the same screenshot (no extra ADB call).
    # Troop pixel positions only make sense on the map screen.
    map_tpl = get_template("elements/map_screen.png")
    if map_tpl is not None:
        result = cv2.matchTemplate(screen, map_tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        if max_val < 0.8:
            log.warning("troops_avail called but not on map_screen (best: %.0f%%) — returning 0", max_val * 100)
            return 0

    def is_yellow(y):
        pixel = screen[y, _TROOP_X].astype(np.int16)
        return np.all(np.abs(pixel - _TROOP_COLOR) < _TROOP_TOLERANCE)

    # Pixel patterns are calibrated for 5-troop accounts.
    # For accounts with fewer total troops, adjust the detected count.
    total = config.DEVICE_TOTAL_TROOPS.get(device, 5)
    offset = 5 - total  # e.g. 4-troop account → offset 1

    for count, pattern in _SLOT_PATTERNS.items():
        if (all(is_yellow(y) for y in pattern["match"]) and
            all(not is_yellow(y) for y in pattern["no_match"])):
            adjusted = max(0, count - offset)
            log.debug("Troops available: %d%s", adjusted,
                      f" (raw {count}, account has {total})" if offset else "")
            return adjusted

    adjusted = max(0, 5 - offset)
    log.debug("Troops available: %d%s", adjusted,
              f" (raw 5, account has {total})" if offset else "")
    return adjusted

def all_troops_home(device):
    """Check if all troops are home (troops_avail matches account total)"""
    total = config.DEVICE_TOTAL_TROOPS.get(device, 5)
    return troops_avail(device) == total

@timed_action("heal_all")
def heal_all(device):
    """Check if we're on map_screen and if heal is needed, then heal all troops.
    Repeats until no more heal button is found (all troops healed)."""
    log = get_logger("troops", device)
    if not navigate("map_screen", device):
        log.warning("Failed to navigate to map screen for healing")
        return False

    healed_any = False
    while tap_image("heal.png", device):
        healed_any = True
        log.debug("Starting heal sequence...")
        time.sleep(1)
        logged_tap(device, 700, 1460, "heal_all_btn")
        time.sleep(1)
        logged_tap(device, 542, 1425, "heal_confirm")
        time.sleep(1)
        logged_tap(device, 1000, 200, "heal_close")
        time.sleep(2)

    if healed_any:
        log.info("Heal sequence complete — all troops healed")
        # Navigate back to map_screen to ensure clean state
        navigate("map_screen", device)
    else:
        log.debug("No healing needed")
    return True
