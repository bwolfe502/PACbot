"""Titan rally and AP restoration.

Dependencies: _helpers (for _last_depart_slot)

Key exports:
    restore_ap     — open AP Recovery menu and restore AP
    rally_titan    — start a titan rally from map screen
"""

import cv2
import time
import os
import re

import config
from config import Screen
from botlog import get_logger, timed_action, stats
from vision import (tap_image, wait_for_image_and_tap, timed_wait,
                    load_screenshot, find_image, get_template,
                    adb_tap, logged_tap,
                    save_failure_screenshot, read_ap,
                    TAP_OFFSETS, _save_click_trail)
from navigation import navigate, check_screen, DEBUG_DIR
from troops import troops_avail, heal_all, capture_departing_portrait

from actions._helpers import _last_depart_slot

_log = get_logger("actions")

_MAX_TITAN_SEARCH_ATTEMPTS = 3


# ============================================================
# AP RESTORE
# ============================================================

# OCR region for AP bar inside the AP Recovery menu (right side where "136/400" shows)
_AP_MENU_REGION = (400, 570, 790, 630)

# AP Recovery menu button coordinates
_AP_FREE_OPEN = (783, 1459)


# Potion coordinates (left to right, smallest to largest, ~168px spacing)
_AP_POTIONS_SMALL = [
    (157, 692),  # 10 AP
    (325, 692),  # 20 AP
    (493, 692),  # 50 AP
]
_AP_POTIONS_LARGE = [
    (661, 692),  # 100 AP
    (829, 692),  # 200 AP
]

# Gem restore button + confirmation
_AP_GEM_BUTTON = (300, 1466)
_AP_GEM_CONFIRM = (774, 1098)
_AP_GEM_COST_REGION = (100, 700, 750, 850)  # OCR region for "Spend X Gem(s)?" text

def _close_ap_menu(device, double_close=True):
    """Close the AP Recovery menu.  When double_close=True (default), also
    closes the search menu behind it (used when restore_ap opened via search).
    Pass double_close=False for game-opened popups with no search menu."""
    tap_image("close_x.png", device)  # Close AP Recovery modal
    time.sleep(0.5)
    if double_close:
        tap_image("close_x.png", device)  # Close search menu
        time.sleep(0.5)

def _read_ap_from_menu(device):
    """Read current/max AP from the AP Recovery menu bar via OCR.
    The AP bar has white text on a dark background, so we invert the image
    before OCR to get reliable slash detection (e.g. '142/400').
    Returns (current, max) tuple or None."""
    log = get_logger("actions", device)
    screen = load_screenshot(device)
    if screen is None:
        log.warning("AP menu OCR: screenshot failed")
        return None

    x1, y1, x2, y2 = _AP_MENU_REGION
    img = screen[y1:y2, x1:x2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    # Threshold to isolate white text and strip outline/shadow artifacts
    # that cause EasyOCR to miss the '/' character
    _, thresh = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)

    # Save debug crop so we can inspect what OCR sees
    cv2.imwrite(os.path.join(DEBUG_DIR, "ap_menu_crop.png"), thresh)
    log.debug("AP menu OCR: saved debug/ap_menu_crop.png (region %s)", _AP_MENU_REGION)

    from vision import ocr_read
    results = ocr_read(thresh, allowlist="0123456789/", detail=0)
    raw = " ".join(results).strip()
    log.debug("AP menu OCR raw: '%s'", raw)

    match = re.search(r"(\d+)/(\d+)", raw)
    if match:
        return int(match.group(1)), int(match.group(2))
    log.warning("AP menu OCR: no 'X/Y' pattern found in '%s'", raw)
    return None

def _read_gem_cost(device):
    """Read the gem cost from the confirmation dialog ('Spend X Gem(s)?').
    Returns the gem cost as an integer, or None if unreadable."""
    log = get_logger("actions", device)
    screen = load_screenshot(device)
    if screen is None:
        return None
    x1, y1, x2, y2 = _AP_GEM_COST_REGION
    img = screen[y1:y2, x1:x2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    from vision import ocr_read
    results = ocr_read(gray, detail=0)
    raw = " ".join(results).strip()
    log.debug("Gem confirmation OCR: '%s'", raw)
    # Look for "Spend X Gem" pattern
    match = re.search(r"(\d[\d,]*)\s*[Gg]em", raw)
    if match:
        return int(match.group(1).replace(",", ""))
    # Fallback: just find any number
    match = re.search(r"(\d[\d,]+)", raw)
    if match:
        return int(match.group(1).replace(",", ""))
    return None

def _restore_ap_from_open_menu(device, needed):
    """Run the AP restoration loop on an already-open AP Recovery menu.

    Tries free restores → potions (smallest first) → gems, respecting config
    flags.  Returns ``(success, current_ap)`` where *success* is True when
    ``current_ap >= needed``.  Does **not** close the menu — the caller
    decides whether to single- or double-close.
    """
    log = get_logger("actions", device)

    ap = _read_ap_from_menu(device)
    if ap is None:
        log.warning("Could not read AP from menu")
        save_failure_screenshot(device, "ap_read_failed")
        return False, 0

    current, maximum = ap
    log.info("Current AP: %d/%d (need %d)", current, maximum, needed)

    if current >= needed:
        return True, current

    # Step 1: Try FREE restore (up to 2 attempts — 25 AP each, 2x daily max)
    if config.AP_USE_FREE and current < needed:
        for free_attempt in range(2):
            if current >= needed:
                break
            log.debug("Trying free AP restore (attempt %d/2)...", free_attempt + 1)
            adb_tap(device, *_AP_FREE_OPEN)  # "OPEN" button
            time.sleep(1.5)

            new_ap = _read_ap_from_menu(device)
            if new_ap is None:
                log.warning("Could not re-read AP after free restore")
                break
            if new_ap[0] > current:
                log.info("Free restore worked: %d -> %d", current, new_ap[0])
                current = new_ap[0]
            else:
                log.debug("Free restore had no effect (exhausted)")
                break

    # Step 2: Try AP potions (smallest first)
    if config.AP_USE_POTIONS and current < needed:
        potions = list(_AP_POTIONS_SMALL)
        potion_labels = ["10", "20", "50"]
        if config.AP_ALLOW_LARGE_POTIONS:
            potions += _AP_POTIONS_LARGE
            potion_labels += ["100", "200"]
        for i, (px, py) in enumerate(potions):
            if current >= needed:
                break
            for use in range(20):  # safety limit
                if current >= needed:
                    break
                log.debug("Trying %s AP potion (use %d)...", potion_labels[i], use + 1)
                adb_tap(device, px, py)
                time.sleep(1.5)

                new_ap = _read_ap_from_menu(device)
                if new_ap is None:
                    log.warning("Could not re-read AP after potion")
                    break
                if new_ap[0] > current:
                    log.info("Potion worked: %d -> %d", current, new_ap[0])
                    current = new_ap[0]
                else:
                    # Retry once — tap may not have registered due to lag
                    log.debug("%s AP potion: no change, retrying tap...", potion_labels[i])
                    adb_tap(device, px, py)
                    time.sleep(1.5)
                    new_ap = _read_ap_from_menu(device)
                    if new_ap is not None and new_ap[0] > current:
                        log.info("Potion worked on retry: %d -> %d", current, new_ap[0])
                        current = new_ap[0]
                    else:
                        log.debug("%s AP potion out of stock", potion_labels[i])
                        break

    # Step 3: Try gem restore (50 AP per use, escalating gem cost, confirmation required)
    # When exhausted, button still shows 3500 but confirmation won't open.
    if config.AP_USE_GEMS and config.AP_GEM_LIMIT > 0 and current < needed:
        gems_spent = 0
        gem_attempts = 0
        while current < needed and gem_attempts < 50:  # safety limit
            gem_attempts += 1
            # Tap gem button — opens confirmation dialog (unless exhausted)
            adb_tap(device, *_AP_GEM_BUTTON)
            time.sleep(1.5)

            # Read the gem cost from "Spend X Gem(s)?" dialog
            gem_cost = _read_gem_cost(device)
            if gem_cost is None:
                log.warning("Gem confirmation did not appear (exhausted or unreadable)")
                break

            if gems_spent + gem_cost > config.AP_GEM_LIMIT:
                log.warning("Gem cost %d would exceed limit (%d+%d > %d), cancelling",
                            gem_cost, gems_spent, gem_cost, config.AP_GEM_LIMIT)
                tap_image("close_x.png", device)  # Close confirmation
                time.sleep(0.5)
                break

            # Confirm the purchase
            log.info("Confirming gem restore (%d gems)...", gem_cost)
            adb_tap(device, *_AP_GEM_CONFIRM)
            time.sleep(1.5)
            gems_spent += gem_cost

            new_ap = _read_ap_from_menu(device)
            if new_ap is None:
                log.warning("Could not re-read AP after gem restore")
                break
            if new_ap[0] > current:
                log.info("Gem restore worked: %d -> %d (%d total gems spent)",
                         current, new_ap[0], gems_spent)
                current = new_ap[0]
            else:
                log.warning("Gem restore had no effect (out of gems?)")
                break

    return current >= needed, current


def restore_ap(device, needed):
    """Open the AP Recovery menu and restore AP until we have at least `needed`.
    Uses free restores first, then AP potions (smallest first).
    Returns True if AP >= needed after restoring, False otherwise.
    """
    log = get_logger("actions", device)
    _ap_start = time.time()
    log.info(">>> restore_ap starting (need %d)...", needed)

    # Navigate to map screen
    if not navigate(Screen.MAP, device):
        log.warning("<<< restore_ap: failed to navigate to map screen (%.1fs)", time.time() - _ap_start)
        stats.record_action(device, "restore_ap", False, time.time() - _ap_start)
        return False

    # Open AP Recovery menu — retry the entire open sequence if it fails
    menu_opened = False
    for open_attempt in range(2):
        if open_attempt > 0:
            log.debug("AP: retrying menu open sequence (attempt %d/2)", open_attempt + 1)
            _close_ap_menu(device)
            time.sleep(0.5)
            if not navigate(Screen.MAP, device):
                return False

        # Tap SEARCH button to open the search/rally menu
        adb_tap(device, 900, 1800)
        time.sleep(1.5)

        # NOTE: Do NOT call check_screen() here — its popup auto-dismiss
        # detects close_x.png on the search menu and closes it before we
        # can tap the AP Recovery button.
        log.debug("AP: search menu tap sent, proceeding to AP Recovery button")

        # Tap the blue lightning bolt button (AP Recovery button in search menu)
        adb_tap(device, 315, 1380)
        time.sleep(1.5)

        # Wait for AP Recovery menu to appear (check for apwindow.png)
        for attempt in range(5):
            screen = load_screenshot(device)
            if screen is not None:
                match = find_image(screen, "apwindow.png", threshold=0.8)
                if match:
                    log.debug("AP Recovery menu detected (attempt %d)", attempt + 1)
                    menu_opened = True
                    break
                else:
                    log.debug("Waiting for AP Recovery menu... (attempt %d/5)", attempt + 1)
            time.sleep(1)

        if menu_opened:
            break

    if not menu_opened:
        log.error("AP Recovery menu did not open after all attempts")
        save_failure_screenshot(device, "ap_menu_failed")
        _close_ap_menu(device)
        return False

    # Delegate to shared restoration helper
    success, current = _restore_ap_from_open_menu(device, needed)

    # Close AP Recovery menu + search menu behind it
    _close_ap_menu(device)

    elapsed = time.time() - _ap_start
    if success:
        log.info("<<< restore_ap completed in %.1fs (%d >= %d)", elapsed, current, needed)
        stats.record_action(device, "restore_ap", True, elapsed)
        return True
    else:
        log.warning("<<< restore_ap failed after %.1fs (%d < %d)", elapsed, current, needed)
        stats.record_action(device, "restore_ap", False, elapsed)
        return False

@timed_action("rally_titan")
def rally_titan(device):
    """Start a titan rally from map screen"""
    log = get_logger("actions", device)
    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    troops = troops_avail(device)
    if troops <= config.MIN_TROOPS_AVAILABLE:
        log.warning("Not enough troops available (have %d, need more than %d)", troops, config.MIN_TROOPS_AVAILABLE)
        return False

    # AP check (if unreadable, proceed anyway — game handles low AP with its own prompt)
    ap = read_ap(device)
    if ap is not None and ap[0] < config.AP_COST_RALLY_TITAN:
        if config.AUTO_RESTORE_AP_ENABLED:
            if not restore_ap(device, config.AP_COST_RALLY_TITAN):
                log.warning("Could not restore enough AP for titan rally")
                return False
        else:
            log.warning("Not enough AP for titan rally (have %d, need %d)", ap[0], config.AP_COST_RALLY_TITAN)
            return False

    if not navigate(Screen.MAP, device):
        log.warning("Failed to navigate to map screen")
        return False

    # Search → center-tap → depart poll, with retry on miss (titan may walk)
    depart_match = None
    depart_screen = None
    for search_attempt in range(_MAX_TITAN_SEARCH_ATTEMPTS):
        if search_attempt > 0:
            log.info("Re-searching for titan (attempt %d/%d)",
                     search_attempt + 1, _MAX_TITAN_SEARCH_ATTEMPTS)
            save_failure_screenshot(device, f"titan_miss_{search_attempt}")
            navigate(Screen.MAP, device)  # clear stale UI before re-search

        # Tap SEARCH button to open rally menu
        logged_tap(device, 900, 1800, "titan_search_btn")

        # Wait for rally menu to open — check if titan select is already visible
        # (may already be on rally tab from a previous search)
        found_select = timed_wait(
            device,
            lambda: find_image(load_screenshot(device), "rally_titan_select.png", threshold=0.5) is not None,
            1.5, "titan_search_menu_open")

        if not found_select:
            # Titan select not visible yet — need to tap rally tab
            logged_tap(device, 850, 560, "titan_rally_tab")
            timed_wait(
                device,
                lambda: find_image(load_screenshot(device), "rally_titan_select.png", threshold=0.5) is not None,
                1.5, "titan_rally_tab_load")

        if not wait_for_image_and_tap("rally_titan_select.png", device, timeout=5, threshold=0.65):
            log.warning("Failed to find Titan select")
            return False
        timed_wait(
            device,
            lambda: find_image(load_screenshot(device), "search.png", threshold=0.6) is not None,
            1, "titan_select_to_search")

        if not wait_for_image_and_tap("search.png", device, timeout=5, threshold=0.65):
            log.warning("Failed to find Search button")
            return False
        timed_wait(
            device,
            lambda: check_screen(device) == Screen.MAP,
            2, "titan_search_complete")

        # Dismiss any popup that appeared during the search (e.g. Season Crystal Card)
        if check_screen(device) != Screen.MAP:
            log.info("Popup appeared after titan search — navigating back to map")
            if not navigate(Screen.MAP, device):
                log.warning("Failed to dismiss popup and return to map")
                return False

        # Select titan on map and confirm
        logged_tap(device, 540, 900, "titan_on_map")
        timed_wait(device, lambda: False, 1.5, "titan_on_map_select")
        logged_tap(device, 420, 1400, "titan_confirm")

        # Wait for deployment panel — poll for depart button
        depart_start = time.time()
        while time.time() - depart_start < 8:
            s = load_screenshot(device)
            if s is not None:
                match = find_image(s, "depart.png", threshold=0.6)
                if match is not None:
                    depart_match = match
                    depart_screen = s
                    break
            time.sleep(0.4)

        if depart_match is not None:
            break  # found depart — proceed to tap it

        log.warning("Depart not found — titan may have walked (attempt %d/%d)",
                    search_attempt + 1, _MAX_TITAN_SEARCH_ATTEMPTS)

    if depart_match is not None:
        # Let the deployment panel fully settle before interacting
        timed_wait(device, lambda: False, 1, "titan_depart_settle")
        try:
            portrait_result = capture_departing_portrait(device, screen=depart_screen)
            if portrait_result:
                _last_depart_slot[device] = portrait_result[0]
                log.debug("Titan departing troop: slot %d", portrait_result[0])
        except Exception:
            log.debug("Portrait capture failed — proceeding without slot tracking")
        # Re-find depart after settle to get fresh coordinates
        s = load_screenshot(device)
        if s is not None:
            fresh_match = find_image(s, "depart.png", threshold=0.6)
            if fresh_match is not None:
                depart_match = fresh_match
                depart_screen = s
        _, max_loc, h, w = depart_match
        dx, dy = TAP_OFFSETS.get("depart.png", (0, 0))
        cx, cy = max_loc[0] + w // 2 + dx, max_loc[1] + h // 2 + dy
        _save_click_trail(depart_screen, device, cx, cy, "depart")
        adb_tap(device, cx, cy)
        log.info("Titan rally started! (depart tapped at %d,%d)", cx, cy)
        return True
    log.warning("Failed to find depart button after %d search attempts", _MAX_TITAN_SEARCH_ATTEMPTS)
    save_failure_screenshot(device, "titan_depart_fail")
    return False
