import cv2
import os
import time
from datetime import datetime

from vision import tap_image, tap, load_screenshot, adb_tap, get_template, timed_wait
import config
from config import Screen
from botlog import get_logger, stats

# ============================================================
# DEBUG DIRECTORY
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(SCRIPT_DIR, "debug")
os.makedirs(DEBUG_DIR, exist_ok=True)

# Keep only the last N debug screenshots to avoid filling disk
def _cleanup_debug_dir(max_files=config.DEBUG_SCREENSHOT_MAX):
    try:
        files = sorted(
            [os.path.join(DEBUG_DIR, f) for f in os.listdir(DEBUG_DIR) if f.endswith(".png")],
            key=os.path.getmtime
        )
        while len(files) > max_files:
            os.remove(files.pop(0))
    except Exception as e:
        get_logger("navigation").warning("Debug dir cleanup failed: %s", e)

def _save_debug_screenshot(device, label, screen=None):
    """Save a screenshot to the debug/ folder with a timestamp and label."""
    try:
        if screen is None:
            screen = load_screenshot(device)
        if screen is None:
            return None
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_device = device.replace(":", "_")
        filename = f"{timestamp}_{safe_device}_{label}.png"
        filepath = os.path.join(DEBUG_DIR, filename)
        cv2.imwrite(filepath, screen)
        log = get_logger("navigation", device)
        log.debug("Debug screenshot saved: debug/%s", filename)
        _cleanup_debug_dir()
        return filepath
    except Exception as e:
        log = get_logger("navigation", device)
        log.warning("Failed to save debug screenshot: %s", e)
        return None

# ============================================================
# SCREEN DETECTION
# ============================================================

SCREEN_TEMPLATES = [
    Screen.MAP, Screen.BATTLE_LIST, Screen.ALLIANCE_QUEST, Screen.TROOP_DETAIL,
    Screen.TERRITORY, Screen.WAR, Screen.PROFILE, Screen.ALLIANCE, Screen.KINGDOM,
]

# Search regions per screen template — (x1, y1, x2, y2).
# Constrains matchTemplate to where the unique marker actually appears,
# reducing work by 50-90% per template vs full 1080x1920 search.
# Templates without an entry fall back to full-image search.
SCREEN_REGIONS = {
    Screen.MAP:            (720, 1780, 1080, 1920),   # bottom-right corner
    Screen.BATTLE_LIST:    (143, 712, 359, 1661),      # tight: 176x909 tpl @ fixed (251,1186)
    Screen.ALLIANCE_QUEST: (0, 361, 1080, 595),        # tight: 1080x194 tpl @ fixed (540,478)
    Screen.TROOP_DETAIL:   (0, 1720, 1080, 1920),      # bottom 200px, full width
    Screen.TERRITORY:      (0, 0, 540, 960),            # top-left quadrant
    Screen.WAR:            (62, 0, 688, 545),           # tight: 302x236 tpl @ x:233-517 y:118-407
    Screen.PROFILE:        (0, 960, 1080, 1920),         # bottom half
    Screen.ALLIANCE:       (17, 1124, 356, 1316),       # tight: 299x152 tpl @ fixed (186,1220)
    Screen.KINGDOM:        (0, 1825, 230, 1920),        # tight: 205x75 tpl @ fixed (107,1882)
}

# Popup templates that overlay the screen and block taps.
# Checked by check_screen() before screen matching — auto-dismissed if found.
# Format: (template_path, log_name, match_threshold)
POPUP_DISMISS_TEMPLATES = [
    ("elements/cancel.png", "QUIT DIALOG", 0.8),    # "Are you sure you want to leave?"
    ("elements/close_x.png", "POPUP (red X)", 0.85), # review popups, misc dialogs
]

def check_screen(device):
    """Takes a screenshot and figures out what screen we're on.
    Checks ALL templates and picks the one with the highest confidence
    to avoid false positives from partial matches.
    Logs ALL match scores for debugging."""
    log = get_logger("navigation", device)
    try:
        screen = load_screenshot(device)
        if screen is None:
            log.warning("Failed to load screenshot")
            return Screen.UNKNOWN

        # Check for logout/disconnection popup first
        attention_template = get_template("elements/attention.png")
        if attention_template is not None:
            result = cv2.matchTemplate(screen, attention_template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            if max_val > 0.8:
                log.error("LOGGED OUT — 'ATTENTION' popup detected")
                log.info("Stopping all tasks...")
                # Stop all running tasks by setting their stop events
                for key, info in list(config.running_tasks.items()):
                    if isinstance(info, dict) and "stop_event" in info:
                        info["stop_event"].set()
                return Screen.LOGGED_OUT

        # Check for popups that overlay the screen and block all taps.
        for tpl_path, popup_name, threshold in POPUP_DISMISS_TEMPLATES:
            tpl = get_template(tpl_path)
            if tpl is None:
                continue
            result = cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED)
            _, tpl_val, _, tpl_loc = cv2.minMaxLoc(result)
            if tpl_val > threshold:
                h, w = tpl.shape[:2]
                cx = tpl_loc[0] + w // 2
                cy = tpl_loc[1] + h // 2
                log.info("*** %s detected (%.0f%%) — auto-dismissing ***", popup_name, tpl_val * 100)
                adb_tap(device, cx, cy)
                time.sleep(1.5)
                screen = load_screenshot(device)
                if screen is None:
                    log.warning("Screenshot failed after popup dismiss")
                    return Screen.UNKNOWN
                log.debug("Popup dismissed, re-scanning screen...")
                break  # Only dismiss one popup per check cycle

        scores = {}
        best_name = None
        best_val = 0.0

        for screen_name in SCREEN_TEMPLATES:
            element = get_template(f"elements/{screen_name}.png")
            if element is None:
                continue

            region = SCREEN_REGIONS.get(screen_name)
            if region:
                rx1, ry1, rx2, ry2 = region
                search_area = screen[ry1:ry2, rx1:rx2]
            else:
                search_area = screen

            result = cv2.matchTemplate(search_area, element, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            scores[screen_name] = max_val

            if max_val > best_val:
                best_val = max_val
                best_name = screen_name
                best_loc = max_loc
                best_region = region
                best_hw = element.shape[:2]

        # Log scores sorted by confidence
        score_str = " | ".join(f"{name}: {val*100:.0f}%" for name, val in
                               sorted(scores.items(), key=lambda x: x[1], reverse=True))
        log.debug("Screen scores: %s", score_str)

        if best_val > config.SCREEN_MATCH_THRESHOLD and best_name is not None:
            # Record hit position for region analysis
            h, w = best_hw
            cx, cy = best_loc[0] + w // 2, best_loc[1] + h // 2
            if best_region:
                cx += best_region[0]
                cy += best_region[1]
            stats.record_template_hit(
                device, f"{best_name}.png", cx, cy, best_val)
            log.debug("Screen identified: %s (%.0f%%)", best_name, best_val * 100)
            return best_name

        # Unknown screen — save debug screenshot automatically
        log.warning("Unknown screen detected (best: %s at %.0f%%)", best_name, best_val * 100)
        _save_debug_screenshot(device, "unknown_screen", screen)
        return Screen.UNKNOWN

    except Exception as e:
        log.error("Exception in check_screen: %s", e, exc_info=True)
        return Screen.UNKNOWN

# ============================================================
# NAVIGATION HELPERS
# ============================================================

def _verify_screen(target_screen, device, wait_time=1.5, retries=2):
    """Verify we arrived at the target screen, with retries."""
    log = get_logger("navigation", device)
    timed_wait(device, lambda: check_screen(device) == target_screen,
               wait_time, f"verify_{target_screen}")
    current = Screen.UNKNOWN
    for attempt in range(1 + retries):
        current = check_screen(device)
        if current == target_screen:
            return True
        if attempt < retries:
            log.debug("Not on %s yet (on %s), retry %d/%d...", target_screen, current, attempt + 1, retries)
            time.sleep(1)
    log.warning("Navigation verify FAILED: expected %s, on %s", target_screen, current)
    stats.record_nav_failure(device, current, target_screen)
    _save_debug_screenshot(device, f"verify_fail_{target_screen}")
    return False

def _recover_to_known_screen(device):
    """Try multiple dismiss strategies to escape unknown screens (popups, dialogs, etc).
    Returns the screen name if recovery succeeds, or 'unknown' if all attempts fail."""
    log = get_logger("navigation", device)
    log.info("On unknown screen, attempting recovery...")

    # Strategy 1: Try tapping cancel button (dismisses confirmation dialogs)
    # Strategy 2: Try tapping red X at top-right of popups
    # Strategy 3: Tap back arrow (template match)
    # Each strategy gets one attempt before moving to the next
    strategies = [
        ("close X (template)", lambda: tap_image("close_x.png", device, threshold=0.7)),
        ("cancel button", lambda: tap_image("cancel.png", device, threshold=0.65)),
        ("back arrow", lambda: tap_image("back_arrow.png", device, threshold=0.7)),
        ("back arrow (retry)", lambda: tap_image("back_arrow.png", device, threshold=0.7)),
    ]

    for name, action in strategies:
        action()
        timed_wait(device, lambda: check_screen(device) != Screen.UNKNOWN,
                   1.5, f"recover_{name}")
        current = check_screen(device)
        if current != Screen.UNKNOWN:
            log.info("Recovery via %s: now on %s", name, current)
            return current
        log.debug("Recovery via %s: still unknown", name)

    log.warning("Recovery FAILED after all strategies")
    _save_debug_screenshot(device, "recovery_fail")
    return Screen.UNKNOWN

# ============================================================
# NAVIGATION
# ============================================================

def navigate(target_screen, device, _depth=0):
    """Navigate from current screen to target screen.
    Returns True only after VERIFYING arrival at target_screen."""
    log = get_logger("navigation", device)

    # Guard against infinite recursion
    if _depth > 3:
        log.error("Navigation recursion limit reached, aborting")
        return False

    current = check_screen(device)
    log.info("Navigating: %s -> %s", current, target_screen)

    # Handle unknown screen at entry — try recovery first
    if current == Screen.UNKNOWN:
        current = _recover_to_known_screen(device)
        if current == Screen.UNKNOWN:
            return False
        if current == target_screen:
            return True

    if current == target_screen:
        log.debug("Already on %s", target_screen)
        return True

    # If on td_screen or alliance_screen, always go to map_screen first
    if current == Screen.TROOP_DETAIL and target_screen != Screen.MAP:
        log.debug("On td_screen, going to map_screen first...")
        adb_tap(device, 990, 1850)
        timed_wait(device, lambda: check_screen(device) == Screen.MAP,
                   2, "nav_td_to_map")
        current = check_screen(device)
    elif current == Screen.ALLIANCE and target_screen != Screen.MAP and target_screen != Screen.WAR:
        log.debug("On alliance_screen, going to map_screen first...")
        tap_image("back_arrow.png", device, threshold=0.7)
        current = check_screen(device)

    # To map_screen - always go back
    if target_screen == Screen.MAP:
        if current == Screen.TROOP_DETAIL:
            adb_tap(device, 990, 1850)
            timed_wait(device, lambda: check_screen(device) == Screen.MAP,
                       2, "nav_td_exit_to_map")
            current = check_screen(device)
        elif current == Screen.ALLIANCE:
            tap_image("back_arrow.png", device, threshold=0.7)
            current = check_screen(device)
        elif current == Screen.KINGDOM:
            # Bottom-right globe icon takes us back to map
            adb_tap(device, 970, 1880)
            timed_wait(device, lambda: check_screen(device) == Screen.MAP,
                       1, "nav_kingdom_to_map")
            current = check_screen(device)
        elif current in [Screen.BATTLE_LIST, Screen.ALLIANCE_QUEST, Screen.WAR, Screen.TERRITORY, Screen.PROFILE]:
            tap_image("back_arrow.png", device, threshold=0.7)
            current = check_screen(device)
            if current == Screen.ALLIANCE:
                # Went through alliance_screen on the way back from war_screen
                tap_image("back_arrow.png", device, threshold=0.7)
                current = check_screen(device)
            if current != Screen.MAP:
                # Landed on a different screen (e.g. td_screen from profile) —
                # recurse so the correct handler is used for this screen.
                return navigate(Screen.MAP, device, _depth=_depth + 1)
        else:
            # Unknown or unhandled screen — try recovery
            current = _recover_to_known_screen(device)
            if current == Screen.MAP:
                return True
            if current != Screen.UNKNOWN:
                return navigate(Screen.MAP, device, _depth=_depth + 1)
            return False
        return current == Screen.MAP

    # To bl_screen
    if target_screen == Screen.BATTLE_LIST:
        if current == Screen.MAP:
            if not tap_image("bl_button.png", device):
                _save_debug_screenshot(device, "bl_button_not_found")
        elif current in [Screen.ALLIANCE_QUEST, Screen.TERRITORY]:
            tap_image("back_arrow.png", device, threshold=0.7)
        else:
            if not navigate(Screen.MAP, device, _depth=_depth + 1):
                return False
            if not tap_image("bl_button.png", device):
                _save_debug_screenshot(device, "bl_button_not_found")
        return _verify_screen(Screen.BATTLE_LIST, device)

    # To aq_screen
    if target_screen == Screen.ALLIANCE_QUEST:
        if current != Screen.BATTLE_LIST:
            if not navigate(Screen.BATTLE_LIST, device, _depth=_depth + 1):
                return False
        tap("quest_button", device)
        return _verify_screen(Screen.ALLIANCE_QUEST, device)

    # To war_screen
    if target_screen == Screen.WAR:
        if current == Screen.ALLIANCE:
            # Already on the alliance screen — just tap the war button
            if not tap_image("alliance_screen.png", device):
                log.debug("alliance_screen.png not found, trying blind tap for war button")
                adb_tap(device, 550, 170)
            return _verify_screen(Screen.WAR, device)
        if current != Screen.MAP:
            if not navigate(Screen.MAP, device, _depth=_depth + 1):
                return False
        adb_tap(device, 640, 1865)
        timed_wait(device, lambda: check_screen(device) == Screen.ALLIANCE,
                   2, "nav_map_to_alliance")
        adb_tap(device, 200, 1200)
        timed_wait(device, lambda: check_screen(device) == Screen.ALLIANCE,
                   1, "nav_alliance_menu_load")
        adb_tap(device, 550, 170)
        return _verify_screen(Screen.WAR, device)

    # To territory_screen
    if target_screen == Screen.TERRITORY:
        if current != Screen.BATTLE_LIST:
            if not navigate(Screen.BATTLE_LIST, device, _depth=_depth + 1):
                return False
        adb_tap(device, 316, 1467)
        return _verify_screen(Screen.TERRITORY, device)

    # To kingdom_screen
    if target_screen == Screen.KINGDOM:
        if current != Screen.MAP:
            if not navigate(Screen.MAP, device, _depth=_depth + 1):
                return False
        adb_tap(device, 75, 1880)
        return _verify_screen(Screen.KINGDOM, device)

    log.warning("Unknown target screen: %s", target_screen)
    return False
