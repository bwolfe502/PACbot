import cv2
import os
import time
from datetime import datetime

from vision import tap_image, tap, load_screenshot, adb_tap, get_template
import config
from botlog import get_logger, stats

# ============================================================
# DEBUG DIRECTORY
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(SCRIPT_DIR, "debug")
os.makedirs(DEBUG_DIR, exist_ok=True)

# Keep only the last 50 debug screenshots to avoid filling disk
def _cleanup_debug_dir(max_files=50):
    try:
        files = sorted(
            [os.path.join(DEBUG_DIR, f) for f in os.listdir(DEBUG_DIR) if f.endswith(".png")],
            key=os.path.getmtime
        )
        while len(files) > max_files:
            os.remove(files.pop(0))
    except Exception:
        pass

def _save_debug_screenshot(device, label, screen=None):
    """Save a screenshot to the debug/ folder with a timestamp and label."""
    try:
        if screen is None:
            screen = load_screenshot(device)
        if screen is None:
            return None
        timestamp = datetime.now().strftime("%H%M%S")
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

SCREEN_TEMPLATES = ["map_screen", "bl_screen", "aq_screen", "td_screen", "territory_screen", "war_screen", "profile_screen", "alliance_screen"]

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
            return "unknown"

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
                return "logged_out"

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
                    return "unknown"
                break  # Only dismiss one popup per check cycle

        scores = {}
        best_name = None
        best_val = 0.0

        for screen_name in SCREEN_TEMPLATES:
            element = get_template(f"elements/{screen_name}.png")
            if element is None:
                continue

            result = cv2.matchTemplate(screen, element, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            scores[screen_name] = max_val

            if max_val > best_val:
                best_val = max_val
                best_name = screen_name

        # Log scores sorted by confidence
        score_str = " | ".join(f"{name}: {val*100:.0f}%" for name, val in
                               sorted(scores.items(), key=lambda x: x[1], reverse=True))
        log.debug("Screen scores: %s", score_str)

        if best_val > 0.8 and best_name is not None:
            return best_name

        # Unknown screen — save debug screenshot automatically
        log.warning("Unknown screen detected (best: %s at %.0f%%)", best_name, best_val * 100)
        _save_debug_screenshot(device, "unknown_screen", screen)
        return "unknown"

    except Exception as e:
        log.error("Exception in check_screen: %s", e, exc_info=True)
        return "unknown"

# ============================================================
# NAVIGATION HELPERS
# ============================================================

def _verify_screen(target_screen, device, wait_time=1.5, retries=2):
    """Verify we arrived at the target screen, with retries."""
    log = get_logger("navigation", device)
    time.sleep(wait_time)
    current = "unknown"
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
    # Strategy 3: Tap back button (75, 75)
    # Each strategy gets one attempt before moving to the next
    strategies = [
        ("close X (template)", lambda: tap_image("close_x.png", device, threshold=0.7)),
        ("cancel button", lambda: tap_image("cancel.png", device, threshold=0.65)),
        ("back button", lambda: adb_tap(device, 75, 75)),
        ("back button (retry)", lambda: adb_tap(device, 75, 75)),
    ]

    for name, action in strategies:
        action()
        time.sleep(1.5)
        current = check_screen(device)
        if current != "unknown":
            log.info("Recovery via %s: now on %s", name, current)
            return current
        log.debug("Recovery via %s: still unknown", name)

    log.warning("Recovery FAILED after all strategies")
    _save_debug_screenshot(device, "recovery_fail")
    return "unknown"

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
    log.debug("Currently on %s, navigating to %s...", current, target_screen)

    # Handle unknown screen at entry — try recovery first
    if current == "unknown":
        current = _recover_to_known_screen(device)
        if current == "unknown":
            return False
        if current == target_screen:
            return True

    if current == target_screen:
        log.debug("Already on %s", target_screen)
        return True

    # If on td_screen or alliance_screen, always go to map_screen first
    if current == "td_screen" and target_screen != "map_screen":
        log.debug("On td_screen, going to map_screen first...")
        adb_tap(device, 990, 1850)
        time.sleep(1)
        current = check_screen(device)
    elif current == "alliance_screen" and target_screen != "map_screen" and target_screen != "war_screen":
        log.debug("On alliance_screen, going to map_screen first...")
        adb_tap(device, 75, 75)
        time.sleep(1)
        current = check_screen(device)

    # To map_screen - always go back
    if target_screen == "map_screen":
        if current == "td_screen":
            adb_tap(device, 990, 1850)
            time.sleep(1)
            current = check_screen(device)
        elif current == "alliance_screen":
            adb_tap(device, 75, 75)
            time.sleep(1)
            current = check_screen(device)
        elif current in ["bl_screen", "aq_screen", "war_screen", "territory_screen", "profile_screen"]:
            adb_tap(device, 75, 75)
            time.sleep(1)
            current = check_screen(device)
            if current == "alliance_screen":
                # Went through alliance_screen on the way back from war_screen
                adb_tap(device, 75, 75)
                time.sleep(1)
                current = check_screen(device)
            if current != "map_screen":
                # Landed on a different screen (e.g. td_screen from profile) —
                # recurse so the correct handler is used for this screen.
                return navigate("map_screen", device, _depth=_depth + 1)
        else:
            # Unknown or unhandled screen — try recovery
            current = _recover_to_known_screen(device)
            if current == "map_screen":
                return True
            if current != "unknown":
                return navigate("map_screen", device, _depth=_depth + 1)
            return False
        return current == "map_screen"

    # To bl_screen
    if target_screen == "bl_screen":
        if current == "map_screen":
            if not tap_image("bl_button.png", device):
                _save_debug_screenshot(device, "bl_button_not_found")
        elif current in ["aq_screen", "territory_screen"]:
            adb_tap(device, 75, 75)
        else:
            if not navigate("map_screen", device, _depth=_depth + 1):
                return False
            if not tap_image("bl_button.png", device):
                _save_debug_screenshot(device, "bl_button_not_found")
        return _verify_screen("bl_screen", device)

    # To aq_screen
    if target_screen == "aq_screen":
        if current != "bl_screen":
            if not navigate("bl_screen", device, _depth=_depth + 1):
                return False
        tap("quest_button", device)
        return _verify_screen("aq_screen", device)

    # To war_screen
    if target_screen == "war_screen":
        if current == "alliance_screen":
            # Already on the alliance screen — just tap the war button
            if not tap_image("alliance_screen.png", device):
                log.debug("alliance_screen.png not found, trying blind tap for war button")
                adb_tap(device, 550, 170)
            return _verify_screen("war_screen", device)
        if current != "map_screen":
            if not navigate("map_screen", device, _depth=_depth + 1):
                return False
        adb_tap(device, 640, 1865)
        time.sleep(1)
        adb_tap(device, 200, 1200)
        time.sleep(1)
        adb_tap(device, 550, 170)
        return _verify_screen("war_screen", device)

    # To territory_screen
    if target_screen == "territory_screen":
        if current != "bl_screen":
            if not navigate("bl_screen", device, _depth=_depth + 1):
                return False
        adb_tap(device, 316, 1467)
        return _verify_screen("territory_screen", device)

    log.warning("Unknown target screen: %s", target_screen)
    return False
