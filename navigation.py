import cv2
import os
import time
from datetime import datetime

from vision import tap_image, tap, load_screenshot, adb_tap, get_template
import config

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
        print(f"[{device}] Debug screenshot saved: debug/{filename}")
        _cleanup_debug_dir()
        return filepath
    except Exception as e:
        print(f"[{device}] Failed to save debug screenshot: {e}")
        return None

# ============================================================
# SCREEN DETECTION
# ============================================================

SCREEN_TEMPLATES = ["map_screen", "bl_screen", "aq_screen", "td_screen", "territory_screen", "war_screen"]

def check_screen(device):
    """Takes a screenshot and figures out what screen we're on.
    Checks ALL templates and picks the one with the highest confidence
    to avoid false positives from partial matches.
    Logs ALL match scores for debugging."""
    try:
        screen = load_screenshot(device)
        if screen is None:
            print(f"[{device}] Failed to load screenshot")
            return "unknown"

        # Check for logout/disconnection popup first
        attention_template = get_template("elements/attention.png")
        if attention_template is not None:
            result = cv2.matchTemplate(screen, attention_template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            if max_val > 0.8:
                print(f"[{device}] *** LOGGED OUT — 'ATTENTION' popup detected ***")
                print(f"[{device}] Stopping all tasks...")
                # Stop all running tasks by setting their stop events
                for key, info in list(config.running_tasks.items()):
                    if isinstance(info, dict) and "stop_event" in info:
                        info["stop_event"].set()
                return "logged_out"

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

            # High confidence — skip remaining templates
            if max_val > 0.9:
                break

        # Log all scores sorted by confidence
        score_str = " | ".join(f"{name}: {val*100:.0f}%" for name, val in
                               sorted(scores.items(), key=lambda x: x[1], reverse=True))
        print(f"[{device}] Screen scores: {score_str}")

        if best_val > 0.8 and best_name is not None:
            return best_name

        # Unknown screen — save debug screenshot automatically
        _save_debug_screenshot(device, "unknown_screen", screen)
        return "unknown"

    except Exception as e:
        print(f"[{device}] Exception in check_screen: {e}")
        return "unknown"

# ============================================================
# NAVIGATION HELPERS
# ============================================================

def _verify_screen(target_screen, device, wait_time=1.5, retries=2):
    """Verify we arrived at the target screen, with retries."""
    time.sleep(wait_time)
    current = "unknown"
    for attempt in range(1 + retries):
        current = check_screen(device)
        if current == target_screen:
            return True
        if attempt < retries:
            print(f"[{device}] Not on {target_screen} yet (on {current}), retry {attempt + 1}/{retries}...")
            time.sleep(1)
    print(f"[{device}] Navigation verify FAILED: expected {target_screen}, on {current}")
    _save_debug_screenshot(device, f"verify_fail_{target_screen}")
    return False

def _recover_to_known_screen(device):
    """Try multiple dismiss strategies to escape unknown screens (popups, dialogs, etc).
    Returns the screen name if recovery succeeds, or 'unknown' if all attempts fail."""
    print(f"[{device}] On unknown screen, attempting recovery...")

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
            print(f"[{device}] Recovery via {name}: now on {current}")
            return current
        print(f"[{device}] Recovery via {name}: still unknown")

    print(f"[{device}] Recovery FAILED after all strategies")
    _save_debug_screenshot(device, "recovery_fail")
    return "unknown"

# ============================================================
# NAVIGATION
# ============================================================

def navigate(target_screen, device, _depth=0):
    """Navigate from current screen to target screen.
    Returns True only after VERIFYING arrival at target_screen."""

    # Guard against infinite recursion
    if _depth > 3:
        print(f"[{device}] Navigation recursion limit reached, aborting")
        return False

    current = check_screen(device)
    print(f"[{device}] Currently on {current}, navigating to {target_screen}...")

    # Handle unknown screen at entry — try recovery first
    if current == "unknown":
        current = _recover_to_known_screen(device)
        if current == "unknown":
            return False
        if current == target_screen:
            return True

    if current == target_screen:
        print(f"[{device}] Already on {target_screen}")
        return True

    # If on td_screen, always go to map_screen first
    if current == "td_screen" and target_screen != "map_screen":
        print(f"[{device}] On td_screen, going to map_screen first...")
        adb_tap(device, 990, 1850)
        time.sleep(1)
        current = check_screen(device)

    # To map_screen - always go back
    if target_screen == "map_screen":
        if current == "td_screen":
            adb_tap(device, 990, 1850)
            time.sleep(1)
        elif current in ["bl_screen", "aq_screen", "war_screen", "territory_screen"]:
            adb_tap(device, 75, 75)
            time.sleep(1)
            current = check_screen(device)
            if current != "map_screen":
                adb_tap(device, 75, 75)
                time.sleep(1)
                current = check_screen(device)
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
            tap_image("bl_button.png", device)
        elif current in ["aq_screen", "territory_screen"]:
            adb_tap(device, 75, 75)
        else:
            if not navigate("map_screen", device, _depth=_depth + 1):
                return False
            tap_image("bl_button.png", device)
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

    print(f"[{device}] Unknown target screen: {target_screen}")
    return False
