"""Evil Guard rally attack sequence.

Dependencies: titans (restore_ap), combat (_detect_player_at_eg),
              _helpers (_interruptible_sleep, _last_depart_slot)

Key exports:
    rally_eg          — full EG rally sequence (dark priests + boss)
    search_eg_reset   — search EG to reset titan distances
    test_eg_positions — diagnostic probe of all EG positions
    EG_PRIEST_POSITIONS — constant positions for EG priests
"""

import cv2
import time
import os

import config
from config import Screen
from botlog import get_logger, timed_action, stats
from vision import (tap_image, wait_for_image_and_tap, timed_wait,
                    load_screenshot, find_image, get_template,
                    adb_tap, adb_swipe, logged_tap,
                    save_failure_screenshot, read_ap)
from navigation import navigate, check_screen, DEBUG_DIR
from troops import (troops_avail, heal_all, read_panel_statuses,
                    TroopAction, capture_departing_portrait)

from actions._helpers import _interruptible_sleep, _last_depart_slot
from actions.titans import restore_ap, _restore_ap_from_open_menu, _close_ap_menu
from actions.combat import _detect_player_at_eg

_log = get_logger("actions")


def _handle_ap_popup(device, needed):
    """Detect and handle the game's AP Recovery popup that appears when
    departing with insufficient AP.

    Returns True if AP was restored (caller can retry depart), False if the
    popup was not detected or restoration failed.
    """
    log = get_logger("actions", device)

    screen = load_screenshot(device)
    if screen is None:
        return False

    match = find_image(screen, "apwindow.png", threshold=0.8)
    if not match:
        return False

    log.warning("AP Recovery popup detected — attempting to restore AP")
    save_failure_screenshot(device, "eg_ap_popup_detected")

    if not config.AUTO_RESTORE_AP_ENABLED:
        log.warning("AUTO_RESTORE_AP not enabled — closing popup and aborting")
        _close_ap_menu(device, double_close=False)
        return False

    success, current = _restore_ap_from_open_menu(device, needed)
    _close_ap_menu(device, double_close=False)  # single close: no search menu behind

    if success:
        log.info("AP restored to %d (need %d) — retrying depart", current, needed)
        return True
    else:
        log.warning("AP restoration failed (%d < %d)", current, needed)
        return False


# Candidate dark priest positions around an EG boss (screen 1080x1920).
# P1 is the EG boss itself (opens first priest dialog).
# P2-P5 are surrounding dark priests. P6 is the final center attack.
EG_PRIEST_POSITIONS = [
    (540, 665),   # P1: EG boss tap (opens first priest dialog)
    (172, 895),   # P2: left-center
    (259, 1213),  # P3: lower-left
    (817, 1213),  # P4: lower-right
    (929, 919),   # P5: right-center
    (540, 913),   # P6: center (final attack / EG boss)
]


def _search_eg_center(device):
    """Navigate to map → open search → rally tab → select EG → search.
    Centers the camera on the nearest Evil Guard. Does NOT close the overlay.
    Returns True if search succeeded, False otherwise."""
    log = get_logger("actions", device)

    if not navigate(Screen.MAP, device):
        log.warning("Failed to navigate to map screen")
        return False

    logged_tap(device, 900, 1800, "eg_search_btn")

    # Wait for rally menu — check if EG select is already visible
    found_select = timed_wait(
        device,
        lambda: find_image(load_screenshot(device), "rally_eg_select.png", threshold=0.5) is not None,
        1.5, "eg_search_menu_open")

    if not found_select:
        # EG select not visible yet — tap rally tab
        logged_tap(device, 850, 560, "eg_rally_tab")
        timed_wait(
            device,
            lambda: find_image(load_screenshot(device), "rally_eg_select.png", threshold=0.6) is not None,
            1.5, "eg_rally_tab_load")

    if not wait_for_image_and_tap("rally_eg_select.png", device, timeout=5, threshold=0.65):
        log.warning("Failed to find Evil Guard select")
        tap_image("close_x.png", device)
        return False
    timed_wait(device, lambda: find_image(load_screenshot(device), "search.png", threshold=0.6) is not None,
               1, "eg_select_to_search")

    if not wait_for_image_and_tap("search.png", device, timeout=5, threshold=0.65):
        log.warning("Failed to find Search button")
        tap_image("close_x.png", device)
        return False
    timed_wait(device, lambda: check_screen(device) == Screen.MAP,
               1, "eg_search_complete")

    return True


def search_eg_reset(device):
    """Search for an Evil Guard without departing to reset titan distances.
    This brings nearby monsters closer again after repeated titan rallies."""
    log = get_logger("actions", device)
    log.info("Searching EG to reset titan distance...")

    if not _search_eg_center(device):
        return False

    # Close out — tap X twice (EG view + search menu)
    tap_image("close_x.png", device)
    time.sleep(0.5)
    tap_image("close_x.png", device)
    time.sleep(0.5)

    log.info("EG search complete — titan distances reset")
    return True

def _probe_priest(device, x, y, label):
    """Tap a candidate priest position and verify the attack dialog opened.

    Saves BEFORE and AFTER screenshots to debug/failures/ for post-mortem.
    Returns True (HIT) if checked.png or unchecked.png appears within 3s.
    Returns False (MISS) and taps back to dismiss any popup on failure.
    """
    log = get_logger("actions", device)

    # Verify we're on the map screen before probing
    current = check_screen(device)
    if current != Screen.MAP:
        log.warning("PROBE %s: on %s instead of map_screen, recovering...", label, current)
        if not navigate(Screen.MAP, device):
            log.warning("PROBE %s: could not recover to map screen", label)
            return False

    # BEFORE screenshot
    save_failure_screenshot(device, f"probe_{label}_BEFORE")

    # Tap the candidate position
    checked_tmpl = get_template("elements/checked.png")
    unchecked_tmpl = get_template("elements/unchecked.png")

    def _dialog_visible():
        s = load_screenshot(device)
        if s is None:
            return False
        if checked_tmpl is not None:
            res = cv2.matchTemplate(s, checked_tmpl, cv2.TM_CCOEFF_NORMED)
            if cv2.minMaxLoc(res)[1] > 0.8:
                return True
        if unchecked_tmpl is not None:
            res = cv2.matchTemplate(s, unchecked_tmpl, cv2.TM_CCOEFF_NORMED)
            if cv2.minMaxLoc(res)[1] > 0.8:
                return True
        return False

    logged_tap(device, x, y, f"probe_{label}")
    timed_wait(device, _dialog_visible, 2.5, "probe_dialog_open")

    start = time.time()
    while time.time() - start < 3:
        screen = load_screenshot(device)
        if screen is None:
            time.sleep(0.5)
            continue

        # Check for checked.png
        if checked_tmpl is not None:
            result = cv2.matchTemplate(screen, checked_tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            if max_val > 0.8:
                log.info("PROBE HIT %s at (%d,%d) — checked %.0f%%", label, x, y, max_val * 100)
                save_failure_screenshot(device, f"probe_{label}_HIT", screen)
                return True

        # Check for unchecked.png
        if unchecked_tmpl is not None:
            result = cv2.matchTemplate(screen, unchecked_tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            if max_val > 0.8:
                log.info("PROBE HIT %s at (%d,%d) — unchecked %.0f%%", label, x, y, max_val * 100)
                save_failure_screenshot(device, f"probe_{label}_HIT", screen)
                return True

        time.sleep(0.5)

    # MISS — no dialog appeared
    log.info("PROBE MISS %s at (%d,%d) — no dialog after 3s", label, x, y)
    save_failure_screenshot(device, f"probe_{label}_MISS")

    # tap_image won't tap if back arrow isn't visible (e.g. on map screen)
    tap_image("back_arrow.png", device, threshold=0.7)
    return False


@timed_action("rally_eg")
def rally_eg(device, stop_check=None):
    """Start an evil guard rally attacking dark priests around an EG.

    Uses probe-and-verify: taps each candidate position, checks if the attack
    dialog opened, and skips positions where no priest exists (killed by other
    players or blocked by UI).  Attacks whatever priests are available instead
    of aborting on the first miss.

    stop_check: optional callable returning True if we should abort early.
    Persistent screenshots saved to debug/failures/ at every probe and failure.
    """
    log = get_logger("actions", device)
    log.debug("rally_eg() called")
    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    troops = troops_avail(device)
    if troops <= config.MIN_TROOPS_AVAILABLE:
        log.warning("Not enough troops available (have %d, need more than %d)", troops, config.MIN_TROOPS_AVAILABLE)
        return False

    # AP check (if unreadable, proceed anyway — game handles low AP with its own prompt)
    ap = read_ap(device)
    log.debug("EG rally: AP = %s", ap)
    if ap is not None and ap[0] < config.AP_COST_EVIL_GUARD:
        if config.AUTO_RESTORE_AP_ENABLED:
            if not restore_ap(device, config.AP_COST_EVIL_GUARD):
                log.warning("Could not restore enough AP for evil guard rally")
                return False
        else:
            log.warning("Not enough AP for evil guard rally (have %d, need %d)", ap[0], config.AP_COST_EVIL_GUARD)
            return False

    # Search EG once — centers camera, stays centered for all priests
    config.set_device_status(device, "Searching for Evil Guard...")
    if not _search_eg_center(device):
        save_failure_screenshot(device, "eg_search_failed")
        return False

    # Wait for search overlay to fully close and camera to settle
    timed_wait(device, lambda: check_screen(device) == Screen.MAP,
               1.5, "eg_search_overlay_close")
    # Verify we're back on map_screen (search overlay dismissed)
    if check_screen(device) != Screen.MAP:
        log.debug("EG: search overlay may still be open, waiting...")
        timed_wait(device, lambda: check_screen(device) == Screen.MAP,
                   1.5, "eg_search_overlay_close_retry")
        if check_screen(device) != Screen.MAP:
            log.warning("EG: not on map_screen after search — recovering")
            if not navigate(Screen.MAP, device):
                return False

    # Check all priest positions for another player's name before attacking.
    # Player names show as blue text (#4296FF) with gold alliance tags (#FFD773).
    screen = load_screenshot(device)
    if screen is not None:
        for idx, (px, py) in enumerate(EG_PRIEST_POSITIONS[:5]):
            if _detect_player_at_eg(screen, px, py):
                log.warning("EG occupied — another player detected near P%d (%d,%d), skipping",
                            idx + 1, px, py)
                save_failure_screenshot(device, f"eg_occupied_P{idx+1}", screen)
                return False

    # Pre-load templates used in inner loops
    checked_img = get_template("elements/checked.png")
    unchecked_img = get_template("elements/unchecked.png")
    stationed_img = get_template("elements/stationed.png")

    def _dialog_visible():
        s = load_screenshot(device)
        if s is None:
            return False
        if checked_img is not None:
            res = cv2.matchTemplate(s, checked_img, cv2.TM_CCOEFF_NORMED)
            if cv2.minMaxLoc(res)[1] > 0.8:
                return True
        if unchecked_img is not None:
            res = cv2.matchTemplate(s, unchecked_img, cv2.TM_CCOEFF_NORMED)
            if cv2.minMaxLoc(res)[1] > 0.8:
                return True
        return False

    # Tap EG boss on map to enter the priest view
    log.debug("EG rally: tapping EG on map")
    logged_tap(device, EG_PRIEST_POSITIONS[0][0], EG_PRIEST_POSITIONS[0][1], "eg_boss_on_map")
    timed_wait(device, _dialog_visible, 2.0, "eg_boss_dialog_open")

    # Region constraint for stationed.png: only search center-right of screen
    # to avoid false positives on the hero portrait list (left side, x < 300).
    _STATIONED_REGION = (300, 500, 1080, 1300)

    def check_and_proceed(priest_num):
        """Find the checkbox (checked/unchecked) and tap Proceed.
        Saves persistent failure screenshot if all attempts exhausted."""
        for attempt in range(10):
            screen = load_screenshot(device)
            if checked_img is not None and screen is not None:
                result = cv2.matchTemplate(screen, checked_img, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)

                if max_val > 0.8:
                    log.debug("P%d: checked found at (%d,%d) %.0f%%, tapping proceed",
                              priest_num, max_loc[0], max_loc[1], max_val * 100)
                    logged_tap(device, 540, 1500, f"eg_proceed_p{priest_num}")
                    timed_wait(device,
                               lambda: find_image(load_screenshot(device), "depart.png", threshold=0.75) is not None,
                               2, "eg_proceed_to_depart")
                    return True
                else:
                    log.debug("P%d: check_and_proceed %d/10 — checked best %.0f%%, tapping unchecked",
                              priest_num, attempt + 1, max_val * 100)
                    tap_image("unchecked.png", device)
                    timed_wait(device, _dialog_visible, 2, "eg_unchecked_toggle")
        log.error("P%d: check_and_proceed FAILED after 10 attempts", priest_num)
        save_failure_screenshot(device, f"eg_check_fail_p{priest_num}")
        return False

    def try_stationed_before_depart(priest_num):
        """Tap stationed.png if visible (within region constraint).
        Uses _STATIONED_REGION to avoid hero portrait false positives."""
        start_time = time.time()
        while time.time() - start_time < 3:
            screen = load_screenshot(device)
            if stationed_img is not None and screen is not None:
                match = find_image(screen, "stationed.png", threshold=0.8, region=_STATIONED_REGION)
                if match:
                    max_val, max_loc, h, w = match
                    cx = max_loc[0] + w // 2
                    cy = max_loc[1] + h // 2
                    log.debug("P%d: stationed found at (%d,%d) %.0f%%, tapping",
                              priest_num, cx, cy, max_val * 100)
                    logged_tap(device, cx, cy, f"eg_stationed_p{priest_num}")
                    return True
            time.sleep(0.5)
        log.debug("P%d: stationed not found in 3s (normal — proceeding to depart)", priest_num)
        return False

    def _check_ap_popup_after_depart(priest_num):
        """After tapping depart, check if the game opened the AP Recovery
        popup instead of deploying.  If so, restore AP and retry depart.
        Returns True if troop deployed (or AP restored + re-depart succeeded),
        False if AP popup appeared and could not be resolved."""
        time.sleep(1.0)  # brief wait for popup or deployment animation
        if not _handle_ap_popup(device, config.AP_COST_EVIL_GUARD):
            return True  # no popup — depart succeeded normally

        # AP was restored — retry depart if the deployment screen is still up
        timed_wait(device,
                   lambda: find_image(load_screenshot(device), "depart.png", threshold=0.75) is not None,
                   2, "eg_ap_restored_depart_wait")
        if tap_image("depart.png", device):
            log.info("P%d: depart tapped after AP restore", priest_num)
            time.sleep(1.0)
            # Verify the popup didn't reappear (still not enough AP)
            screen = load_screenshot(device)
            if screen is not None and find_image(screen, "apwindow.png", threshold=0.8):
                log.error("P%d: AP popup reappeared — still insufficient AP", priest_num)
                _close_ap_menu(device, double_close=False)
                return False
            return True
        log.warning("P%d: depart not found after AP restore — deployment screen may have closed", priest_num)
        return False

    def click_depart_with_fallback(priest_num):
        """Tap the depart button with retries. Captures departing portrait on
        first attempt for troop identity tracking. Detects AP Recovery popup
        and restores AP if needed. Saves failure screenshot on exhaustion."""
        for attempt in range(5):
            if attempt == 0:
                # Capture which troop is being sent (portrait + slot) before departing
                result = capture_departing_portrait(device)
                if result:
                    slot_id, _ = result
                    _last_depart_slot[device] = slot_id
                    log.debug("P%d: departing troop is slot %d", priest_num, slot_id)
            if tap_image("depart.png", device):
                log.debug("P%d: depart tapped (attempt %d)", priest_num, attempt + 1)
                return _check_ap_popup_after_depart(priest_num)
            if tap_image("defending.png", device):
                log.debug("P%d: found defending, retrying depart", priest_num)
                timed_wait(device,
                           lambda: find_image(load_screenshot(device), "depart.png", threshold=0.75) is not None,
                           1, "eg_defending_to_depart")
                if tap_image("depart.png", device):
                    log.debug("P%d: depart tapped after defending", priest_num)
                    return _check_ap_popup_after_depart(priest_num)
            if attempt < 4:
                log.debug("P%d: depart not found, retry %d/5...", priest_num, attempt + 1)
                timed_wait(device,
                           lambda: find_image(load_screenshot(device), "depart.png", threshold=0.75) is not None,
                           2, "eg_depart_retry_wait")
        log.error("P%d: click_depart FAILED after 5 attempts", priest_num)
        save_failure_screenshot(device, f"eg_depart_fail_p{priest_num}")
        return False

    def poll_troop_ready(timeout_seconds, priest_num):
        """Poll map panel for stationed status. Returns True when stationed detected.
        Uses read_panel_statuses for rich status logging (marching → battling →
        stationed transitions), with fallback to raw template matching if panel
        reading fails. Checks stop_check every 3s."""
        log.debug("P%d: polling for stationed (timeout=%ds)...", priest_num, timeout_seconds)
        start_time = time.time()
        last_summary = None
        while time.time() - start_time < timeout_seconds:
            if stop_check and stop_check():
                log.info("P%d: poll_troop_ready aborted (stop requested)", priest_num)
                return False

            # Safety net: detect AP Recovery popup (troop never deployed)
            ap_screen = load_screenshot(device)
            if ap_screen is not None and find_image(ap_screen, "apwindow.png", threshold=0.8):
                log.warning("P%d: AP Recovery popup detected during poll — troop never deployed", priest_num)
                _handle_ap_popup(device, config.AP_COST_EVIL_GUARD)
                return False  # caller should retry the priest

            snapshot = read_panel_statuses(device)
            if snapshot:
                # Log status transitions for deployed troops
                deployed = [t for t in snapshot.troops if not t.is_home]
                summary = ", ".join(t.action.value for t in deployed)
                if summary != last_summary:
                    log.info("P%d: troop status → %s", priest_num, summary)
                    last_summary = summary
                if snapshot.any_doing(TroopAction.STATIONING):
                    elapsed = time.time() - start_time
                    log.debug("P%d: stationed detected via panel after %.1fs", priest_num, elapsed)
                    return True
            else:
                # Fallback: raw template match (may not be on map screen yet)
                screen = load_screenshot(device)
                if stationed_img is not None and screen is not None:
                    result = cv2.matchTemplate(screen, stationed_img, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(result)
                    if max_val > 0.8:
                        elapsed = time.time() - start_time
                        log.debug("P%d: stationed at (%d,%d) %.0f%% after %.1fs (fallback)",
                                  priest_num, max_loc[0], max_loc[1], max_val * 100, elapsed)
                        return True
            # Poll interval — recording for optimization analysis
            stats.record_transition_time(device, "eg_poll_troop_interval", 3, 3, False)
            if _interruptible_sleep(3, stop_check):
                log.info("P%d: poll_troop_ready aborted (stop requested during sleep)", priest_num)
                return False
        elapsed = time.time() - start_time
        log.warning("P%d: poll_troop_ready TIMED OUT after %.1fs", priest_num, elapsed)
        save_failure_screenshot(device, f"eg_stationed_timeout_p{priest_num}")
        return False

    def dismiss_and_verify_map(priest_num):
        """After a rally completes, dismiss any remaining dialog overlay and
        verify we're back on the map screen before tapping the next priest.

        Checks for dialog presence BEFORE tapping to avoid opening the
        profile screen by hitting (75,75) on the map.
        """
        for attempt in range(5):
            screen = load_screenshot(device)
            if screen is None:
                time.sleep(1)
                continue

            # Check if dialog elements are still visible
            dialog_open = False
            if checked_img is not None:
                result = cv2.matchTemplate(screen, checked_img, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(result)
                if max_val > 0.8:
                    dialog_open = True
            if not dialog_open:
                if find_image(screen, "depart.png", threshold=0.75):
                    dialog_open = True

            if not dialog_open:
                # No dialog — verify we're actually on the map screen
                current = check_screen(device)
                if current == Screen.MAP:
                    log.debug("P%d: dialog dismissed, map screen ready", priest_num)
                    return True
                log.debug("P%d: no dialog but on %s, dismissing (attempt %d/5)",
                          priest_num, current, attempt + 1)
            else:
                log.debug("P%d: dialog still open, dismissing (attempt %d/5)",
                          priest_num, attempt + 1)

            tap_image("back_arrow.png", device, threshold=0.7)

        # Last resort: navigate to map
        log.warning("P%d: could not dismiss after 5 attempts, navigating to map", priest_num)
        if navigate(Screen.MAP, device):
            return True
        save_failure_screenshot(device, f"eg_dismiss_fail_p{priest_num}")
        return False

    # =====================================================
    # PRIEST 1 — EG boss tap (probe verifies dialog opened)
    # =====================================================
    config.set_device_status(device, "Killing Dark Priests (1/5)...")
    log.info("P1: probing EG boss at (%d,%d)", *EG_PRIEST_POSITIONS[0])

    # P1 was already tapped above (eg_boss_on_map) — verify dialog opened
    p1_hit = False
    start = time.time()
    while time.time() - start < 3:
        screen = load_screenshot(device)
        if screen is not None and checked_img is not None:
            result = cv2.matchTemplate(screen, checked_img, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            if max_val > 0.8:
                p1_hit = True
                break
        unchecked_tmpl = get_template("elements/unchecked.png")
        if screen is not None and unchecked_tmpl is not None:
            result = cv2.matchTemplate(screen, unchecked_tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            if max_val > 0.8:
                p1_hit = True
                break
        time.sleep(0.5)

    attacks_completed = 0
    priests_dead = 0       # priests confirmed dead (attacked by us OR already dead)

    if p1_hit:
        log.info("P1: PROBE HIT — attack dialog opened")
        save_failure_screenshot(device, "probe_P1_HIT")
        if not check_and_proceed(1):
            return False
        if not click_depart_with_fallback(1):
            return False
        config.set_device_status(device, "Marching to Dark Priest (1/5)...")
        if not poll_troop_ready(240, 1):
            return False
        log.info("P1: rally completed")
        attacks_completed += 1
        priests_dead += 1
    else:
        log.warning("P1: PROBE MISS — no dialog after tapping EG boss")
        save_failure_screenshot(device, "probe_P1_MISS")
        priests_dead += 1   # priest is already dead (killed by others)
        tap_image("back_arrow.png", device, threshold=0.7)

    # =====================================================
    # PRIESTS 2–5 — probe each, collect misses for retry
    # =====================================================
    missed_priests = []
    for i in range(1, 5):  # EG_PRIEST_POSITIONS[1] through [4]
        pnum = i + 1
        x, y = EG_PRIEST_POSITIONS[i]
        config.set_device_status(device, f"Killing Dark Priests ({pnum}/5)...")
        log.info("P%d: probing at (%d, %d)", pnum, x, y)

        # Dismiss any remaining dialog from the previous rally
        if attacks_completed > 0 or p1_hit:
            if not dismiss_and_verify_map(pnum):
                log.warning("P%d: could not dismiss dialog, aborting", pnum)
                return False

        # Probe: tap + verify dialog opened
        if _probe_priest(device, x, y, f"P{pnum}"):
            # HIT — proceed with attack
            if not check_and_proceed(pnum):
                log.warning("P%d: check_and_proceed failed after probe hit — skipping", pnum)
                continue
            try_stationed_before_depart(pnum)
            if not click_depart_with_fallback(pnum):
                log.warning("P%d: depart failed — skipping", pnum)
                continue
            timed_wait(device, lambda: check_screen(device) == Screen.MAP,
                       1, "eg_depart_to_map")
            config.set_device_status(device, f"Killing Dark Priest ({pnum}/5)...")
            if not poll_troop_ready(60, pnum):
                log.warning("P%d: stationed timeout — continuing anyway", pnum)
            log.info("P%d: rally completed", pnum)
            attacks_completed += 1
            priests_dead += 1
        else:
            log.info("P%d: MISS — will retry with camera nudge", pnum)
            missed_priests.append((i, pnum, x, y))

    # =====================================================
    # RETRY MISSED PRIESTS — nudge camera to clear UI occlusion
    # =====================================================
    # Priests near screen edges can be occluded by troop march panels
    # or other UI.  Apply a slow camera drag to shift the priest toward
    # screen center, then re-probe.  We reverse nudges instead of using
    # _search_eg_center() which would navigate to a DIFFERENT evil guard.
    if missed_priests:
        log.info("Retrying %d missed priest(s) with camera nudge...", len(missed_priests))

    center_x, center_y = 540, 960
    accumulated_nudge_dx = 0
    accumulated_nudge_dy = 0

    for (i, pnum, x, y) in missed_priests:
        if stop_check and stop_check():
            log.info("Retry aborted (stop requested)")
            return False

        # Dismiss any open dialog before nudging
        if attacks_completed > 0:
            dismiss_and_verify_map(pnum)

        # Reverse previous retry nudge to get back to original EG center
        # (don't use _search_eg_center — it finds a DIFFERENT evil guard)
        if accumulated_nudge_dx != 0 or accumulated_nudge_dy != 0:
            log.info("P%d retry: reversing previous nudge (%+d, %+d)",
                     pnum, -accumulated_nudge_dx, -accumulated_nudge_dy)
            rev_end_x = max(50, min(1030, center_x - accumulated_nudge_dx))
            rev_end_y = max(50, min(1870, center_y - accumulated_nudge_dy))
            adb_swipe(device, center_x, center_y, rev_end_x, rev_end_y, 1000)
            time.sleep(0.5)
            accumulated_nudge_dx = 0
            accumulated_nudge_dy = 0

        # Calculate nudge to bring priest ~60% toward screen center
        nudge_dx = int((center_x - x) * 0.6)
        nudge_dy = int((center_y - y) * 0.6)
        end_x = max(50, min(1030, center_x + nudge_dx))
        end_y = max(50, min(1870, center_y + nudge_dy))

        log.info("P%d retry: nudging camera by (%+d, %+d)", pnum, nudge_dx, nudge_dy)
        adb_swipe(device, center_x, center_y, end_x, end_y, 1000)
        time.sleep(0.5)
        accumulated_nudge_dx = nudge_dx
        accumulated_nudge_dy = nudge_dy

        # Probe at adjusted position
        adj_x = x + nudge_dx
        adj_y = y + nudge_dy
        config.set_device_status(device, "Retrying Missing Priests...")
        log.info("P%d retry: probing at adjusted (%d, %d)", pnum, adj_x, adj_y)

        if _probe_priest(device, adj_x, adj_y, f"P{pnum}_retry"):
            # HIT on retry — full attack sequence
            log.info("P%d retry: HIT — attacking", pnum)
            if not check_and_proceed(pnum):
                log.warning("P%d retry: check_and_proceed failed — skipping", pnum)
                priests_dead += 1
                continue
            try_stationed_before_depart(pnum)
            if not click_depart_with_fallback(pnum):
                log.warning("P%d retry: depart failed — skipping", pnum)
                priests_dead += 1
                continue
            timed_wait(device, lambda: check_screen(device) == Screen.MAP,
                       1, "eg_depart_to_map")
            config.set_device_status(device, f"Killing Dark Priest ({pnum}/5)...")
            if not poll_troop_ready(60, pnum):
                log.warning("P%d retry: stationed timeout — continuing anyway", pnum)
            log.info("P%d retry: rally completed", pnum)
            attacks_completed += 1
            priests_dead += 1
        else:
            log.info("P%d retry: still MISS — priest truly dead", pnum)
            priests_dead += 1

    if attacks_completed == 0 and not p1_hit:
        log.error("No priests found at any position — aborting")
        save_failure_screenshot(device, "eg_no_priests_found")
        return False

    # =====================================================
    # PRIEST 6 — final attack (special 2-tap flow)
    # All 5 dark priests must be dead before the EG boss can be attacked.
    # A priest counts as dead whether we killed it or it was already dead (MISS).
    # =====================================================
    if priests_dead < 5:
        log.warning("Only %d/5 priests dead (%d attacked by us) — skipping final EG attack",
                    priests_dead, attacks_completed)
        save_failure_screenshot(device, "eg_priests_incomplete")
        return False

    log.info("All 5 priests dead (%d attacked by us, %d already dead) — proceeding to final EG attack",
             attacks_completed, priests_dead - attacks_completed)

    pnum = 6
    x6, y6 = EG_PRIEST_POSITIONS[5]
    config.set_device_status(device, "Rallying Evil Guard...")
    log.info("P6: starting final EG rally at (%d,%d)", x6, y6)

    # If retries shifted the camera, reverse the nudge to re-center before P6
    # (don't use _search_eg_center — it finds a DIFFERENT evil guard)
    if accumulated_nudge_dx != 0 or accumulated_nudge_dy != 0:
        log.info("P6: reversing retry nudge (%+d, %+d) to re-center",
                 -accumulated_nudge_dx, -accumulated_nudge_dy)
        rev_end_x = max(50, min(1030, center_x - accumulated_nudge_dx))
        rev_end_y = max(50, min(1870, center_y - accumulated_nudge_dy))
        adb_swipe(device, center_x, center_y, rev_end_x, rev_end_y, 1000)
        time.sleep(0.5)

    # Dismiss dialog from previous priest
    if not dismiss_and_verify_map(6):
        return False

    # Ensure a troop is available for the EG rally — troops may still be
    # returning from dark priest rallies.  Always poll instead of gating on
    # troops_avail(), which can return 5 (fallback) when pixel patterns
    # don't match and falsely skip the wait.
    config.set_device_status(device, "Waiting for Troop to Return...")
    log.info("P6: checking troop availability before EG rally...")
    wait_start = time.time()
    while time.time() - wait_start < 300:
        if stop_check and stop_check():
            log.info("P6: troop wait aborted (stop requested)")
            return False

        # Primary check: panel status (OCR-based, more reliable)
        snapshot = read_panel_statuses(device)
        if snapshot:
            deployed = [t for t in snapshot.troops if not t.is_home]
            summary = ", ".join(t.action.value for t in deployed)
            if snapshot.home_count > 0:
                log.info("P6: %d troop(s) home (panel) — proceeding with EG rally [%s]",
                         snapshot.home_count, summary)
                break
            log.debug("P6: waiting for troop — %d deployed: %s", len(deployed), summary)

        # Secondary check: pixel-based (skip if it returns max, likely fallback)
        total = config.DEVICE_TOTAL_TROOPS.get(device, 5)
        avail = troops_avail(device)
        if 0 < avail < total:
            log.info("P6: %d troop(s) available (pixel) — proceeding with EG rally", avail)
            break

        if _interruptible_sleep(3, stop_check):
            log.info("P6: troop wait aborted (stop requested during sleep)")
            return False
    else:
        log.warning("P6: no troop available after 300s — aborting EG rally")
        save_failure_screenshot(device, "eg_no_troop_for_p6")
        return False

    # P6 uses a two-tap sequence: tap EG boss, then tap attack confirm.
    # Verify the dialog opened by checking for depart/checked/unchecked/defending.
    # Retry the sequence up to 3 times if the taps don't register.
    p6_dialog_opened = False
    for p6_attempt in range(3):
        logged_tap(device, x6, y6, "eg_final_priest")
        timed_wait(device, lambda: check_screen(device) != Screen.MAP,
                   1, "eg_p6_boss_tap")
        logged_tap(device, 421, 1412, "eg_final_attack")
        timed_wait(device, _dialog_visible, 1, "eg_p6_attack_dialog")

        # Verify the attack dialog appeared
        for _ in range(4):
            s = load_screenshot(device)
            if s is not None:
                if find_image(s, "depart.png", threshold=0.75):
                    p6_dialog_opened = True
                    break
                if find_image(s, "defending.png", threshold=0.8):
                    p6_dialog_opened = True
                    log.debug("P6: dialog detected via defending.png")
                    break
                if checked_img is not None:
                    result = cv2.matchTemplate(s, checked_img, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, _ = cv2.minMaxLoc(result)
                    if max_val > 0.8:
                        p6_dialog_opened = True
                        break
                unchecked_tmpl = get_template("elements/unchecked.png")
                if unchecked_tmpl is not None:
                    result = cv2.matchTemplate(s, unchecked_tmpl, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, _ = cv2.minMaxLoc(result)
                    if max_val > 0.8:
                        p6_dialog_opened = True
                        break
            time.sleep(0.5)

        if p6_dialog_opened:
            log.debug("P6: attack dialog confirmed (attempt %d)", p6_attempt + 1)
            break
        log.warning("P6: dialog not detected after taps (attempt %d/3) — retrying", p6_attempt + 1)
        save_failure_screenshot(device, f"eg_p6_dialog_miss_a{p6_attempt+1}")
        # Dismiss anything that might have opened, return to map
        if check_screen(device) != Screen.MAP:
            tap_image("back_arrow.png", device, threshold=0.7)

    if not p6_dialog_opened:
        log.error("P6: attack dialog never opened after 3 attempts — aborting")
        save_failure_screenshot(device, "eg_p6_dialog_failed")
        return False

    try_stationed_before_depart(6)
    if not click_depart_with_fallback(6):
        return False
    if not poll_troop_ready(240, 6):
        return False
    if not tap_image("stationed.png", device):
        log.error("P6: final stationed tap failed")
        save_failure_screenshot(device, "eg_final_stationed_fail")
        return False

    timed_wait(device,
               lambda: find_image(load_screenshot(device), "return.png", threshold=0.8) is not None,
               2, "eg_p6_stationed_to_return")
    if not tap_image("return.png", device):
        log.warning("EG rally: return button not found")
        save_failure_screenshot(device, "eg_return_fail")
        return False

    attacks_completed += 1
    log.info("Evil Guard rally completed — %d priests attacked!", attacks_completed)
    return True

@timed_action("test_eg_positions")
def test_eg_positions(device):
    """Diagnostic: probe all EG priest positions and report hit/miss.

    Searches for an EG to center the camera, then probes each candidate
    position WITHOUT attacking. Logs a summary table and saves before/after
    screenshots for every probe to debug/failures/.

    Safe to run repeatedly for data collection.
    """
    log = get_logger("actions", device)
    log.info("=== TEST EG POSITIONS — starting diagnostic ===")

    if not _search_eg_center(device):
        log.warning("TEST: could not center on EG — aborting")
        return False

    # Tap EG boss to set up the view (same as rally_eg P1 entry)
    p1_x, p1_y = EG_PRIEST_POSITIONS[0]
    log.info("TEST: tapping EG boss at (%d,%d) to enter priest view", p1_x, p1_y)
    logged_tap(device, p1_x, p1_y, "test_eg_boss")
    time.sleep(1.5)

    results = {}

    # Probe P1 — already tapped above, just check if dialog opened
    checked_tmpl = get_template("elements/checked.png")
    unchecked_tmpl = get_template("elements/unchecked.png")
    p1_hit = False
    save_failure_screenshot(device, "test_probe_P1_BEFORE")
    start = time.time()
    while time.time() - start < 3:
        screen = load_screenshot(device)
        if screen is not None:
            if checked_tmpl is not None:
                result = cv2.matchTemplate(screen, checked_tmpl, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(result)
                if max_val > 0.8:
                    p1_hit = True
                    break
            if unchecked_tmpl is not None:
                result = cv2.matchTemplate(screen, unchecked_tmpl, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(result)
                if max_val > 0.8:
                    p1_hit = True
                    break
        time.sleep(0.5)

    results["P1"] = p1_hit
    status = "HIT" if p1_hit else "MISS"
    log.info("TEST P1 (%d,%d): %s", p1_x, p1_y, status)
    save_failure_screenshot(device, f"test_probe_P1_{status}")

    # Dismiss P1 dialog if hit
    if p1_hit:
        logged_tap(device, 75, 75, "test_dismiss_P1")
        time.sleep(1)

    # Probe P2-P5
    for i in range(1, 5):
        pnum = i + 1
        x, y = EG_PRIEST_POSITIONS[i]
        label = f"P{pnum}"

        # Dismiss any leftover dialog
        logged_tap(device, 75, 75, f"test_dismiss_before_{label}")
        time.sleep(1)

        hit = _probe_priest(device, x, y, f"test_{label}")
        results[label] = hit
        log.info("TEST %s (%d,%d): %s", label, x, y, "HIT" if hit else "MISS")

        # Dismiss dialog if hit (don't attack)
        if hit:
            logged_tap(device, 75, 75, f"test_dismiss_{label}")
            time.sleep(1)

    # Probe P6
    p6_x, p6_y = EG_PRIEST_POSITIONS[5]
    logged_tap(device, 75, 75, "test_dismiss_before_P6")
    time.sleep(1)
    hit = _probe_priest(device, p6_x, p6_y, "test_P6")
    results["P6"] = hit
    log.info("TEST P6 (%d,%d): %s", p6_x, p6_y, "HIT" if hit else "MISS")
    if hit:
        logged_tap(device, 75, 75, "test_dismiss_P6")
        time.sleep(1)

    # Summary
    hits = sum(1 for v in results.values() if v)
    total = len(results)
    summary_lines = [f"  {label}: {'HIT' if hit else 'MISS'}" for label, hit in results.items()]
    log.info("=== TEST EG POSITIONS — %d/%d hit ===\n%s", hits, total, "\n".join(summary_lines))

    return results
