"""Rally joining, war rallies, and rally owner blacklist.

Dependencies: _helpers (for _last_depart_slot)

Key exports:
    join_rally           — join a rally by type(s) on war screen
    join_war_rallies     — join castle/pass/tower war rallies
    reset_rally_blacklist — clear rally owner blacklist
"""

import cv2
import time
import os
import re

import config
from config import QuestType, RallyType, Screen
from botlog import get_logger, timed_action, stats
from vision import (tap_image, wait_for_image_and_tap, timed_wait,
                    load_screenshot, find_image, get_last_best,
                    find_all_matches, get_template,
                    adb_tap, adb_swipe, logged_tap,
                    save_failure_screenshot)
from navigation import navigate, check_screen, DEBUG_DIR
from troops import (troops_avail, heal_all, read_panel_statuses,
                    TroopAction, capture_departing_portrait)

from actions._helpers import _last_depart_slot

_log = get_logger("actions")


def _on_war_screen(device):
    """Check if we're still on the war screen."""
    log = get_logger("actions", device)
    screen = load_screenshot(device)
    if screen is None:
        return False
    if find_image(screen, "war_screen.png", threshold=0.8):
        return True
    log.debug("Not on war screen anymore")
    return False


# ---- Rally owner blacklist ----
# When a rally join fails (e.g. "Cannot march across protected zones"),
# we track consecutive failures per rally owner. After RALLY_BLACKLIST_THRESHOLD
# consecutive failures (or an immediate error message detection), the owner is
# blacklisted for RALLY_BLACKLIST_EXPIRY_S seconds. Blacklist is also cleared
# when auto-quest starts a new cycle.

RALLY_BLACKLIST_THRESHOLD = 2        # consecutive failures before blacklisting
RALLY_BLACKLIST_EXPIRY_S = 30 * 60   # 30 minutes

_rally_owner_blacklist = {}   # {device: {name_lower: blacklisted_timestamp}}
_rally_owner_failures = {}    # {device: {name_lower: consecutive_failure_count}}

def _record_rally_owner_failure(device, owner):
    """Record a failed join for a rally owner. Returns True if now blacklisted."""
    if not owner:
        return False
    name = owner.lower().strip()
    if not name:
        return False
    if device not in _rally_owner_failures:
        _rally_owner_failures[device] = {}
    _rally_owner_failures[device][name] = _rally_owner_failures[device].get(name, 0) + 1
    count = _rally_owner_failures[device][name]
    _log.debug("Rally owner '%s' failure count: %d/%d", owner, count, RALLY_BLACKLIST_THRESHOLD)
    if count >= RALLY_BLACKLIST_THRESHOLD:
        _blacklist_rally_owner(device, owner)
        return True
    return False

def _blacklist_rally_owner(device, owner):
    """Add a rally owner to the blacklist with a timestamp for expiry."""
    if not owner:
        return
    name = owner.lower().strip()
    if not name:
        return
    if device not in _rally_owner_blacklist:
        _rally_owner_blacklist[device] = {}
    _rally_owner_blacklist[device][name] = time.time()
    # Clear failure counter since they're now blacklisted
    if device in _rally_owner_failures:
        _rally_owner_failures[device].pop(name, None)
    _log.warning("Blacklisted rally owner '%s' on %s (expires in %d min)",
                 owner, device, RALLY_BLACKLIST_EXPIRY_S // 60)

def _clear_rally_owner_failures(device, owner):
    """Clear failure counter for an owner after a successful join."""
    if not owner:
        return
    name = owner.lower().strip()
    if device in _rally_owner_failures:
        _rally_owner_failures[device].pop(name, None)

def _is_rally_owner_blacklisted(device, owner):
    """Check if a rally owner is blacklisted (and not expired) for this device."""
    if not owner:
        return False
    name = owner.lower().strip()
    if not name:
        return False
    device_bl = _rally_owner_blacklist.get(device, {})
    if name not in device_bl:
        return False
    # Check expiry
    elapsed = time.time() - device_bl[name]
    if elapsed > RALLY_BLACKLIST_EXPIRY_S:
        del device_bl[name]
        _log.info("Rally owner '%s' blacklist expired on %s (%.0f min ago)", owner, device, elapsed / 60)
        return False
    return True

def reset_rally_blacklist(device=None):
    """Clear the rally owner blacklist and failure counters.
    If device given, clear only that device."""
    if device is None:
        _rally_owner_blacklist.clear()
        _rally_owner_failures.clear()
        _log.info("Rally owner blacklist cleared (all devices)")
    else:
        if device in _rally_owner_blacklist:
            count = len(_rally_owner_blacklist.pop(device))
            _log.info("Rally owner blacklist cleared for %s (%d entries)", device, count)
        _rally_owner_failures.pop(device, None)


# ---- In-game error detection ----

# Error messages flash briefly in a banner across the upper-center of the screen.
# Known error patterns that indicate a permanent/zone-based failure:
_RALLY_ERROR_KEYWORDS = ["cannot", "protected", "march", "zone"]

def _ocr_error_banner(screen):
    """OCR the error banner area (upper-center of the map screen).
    Returns the error text if it matches known error patterns, else empty string."""
    if screen is None:
        return ""
    h, w = screen.shape[:2]
    # Error banners appear in roughly the top 30% of the screen, center area
    y_start = int(h * 0.15)
    y_end = int(h * 0.35)
    x_start = int(w * 0.05)
    x_end = int(w * 0.95)
    crop = screen[y_start:y_end, x_start:x_end]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

    from vision import ocr_read
    results = ocr_read(gray, detail=0)
    text = " ".join(results).strip().lower()
    if not text:
        return ""

    # Check if text matches known error patterns
    if any(kw in text for kw in _RALLY_ERROR_KEYWORDS):
        return text
    return ""


# ============================================================
# RALLY FUNCTIONS
# ============================================================

def join_rally(rally_types, device, skip_heal=False, stop_check=None):
    """Join a rally of any given type(s) by looking for icons on the war screen.
    rally_types: string or list of strings (e.g. "eg" or ["eg", "titan"]).
    Checks all types simultaneously on each scroll position.
    skip_heal: if True, skip the heal_all call (caller already healed).
    stop_check: optional callable that returns True if we should abort immediately.
    Returns the type joined as a string, or False if none found.
    """
    log = get_logger("actions", device)
    _jr_start = time.time()
    if isinstance(rally_types, str):
        rally_types = [rally_types]
    log.info(">>> join_rally starting (types: %s)", "/".join(rally_types))

    if config.AUTO_HEAL_ENABLED and not skip_heal:
        heal_all(device)

    troops = troops_avail(device)
    if troops <= config.MIN_TROOPS_AVAILABLE:
        log.warning("Not enough troops available (have %d, need more than %d)", troops, config.MIN_TROOPS_AVAILABLE)
        return False

    # Capture a tighter baseline right before entering the war screen.
    # The initial `troops` check above may be stale by the time we tap depart.
    pre_war_troops = troops
    log.debug("Troop baseline before war_screen: %d", pre_war_troops)

    # Snapshot rallying count for join verification (troops-up disambiguation)
    pre_snapshot = read_panel_statuses(device)
    pre_rallying = len(pre_snapshot.troops_by_action(TroopAction.RALLYING)) if pre_snapshot else 0

    if not navigate(Screen.WAR, device):
        log.warning("Failed to navigate to war screen")
        return False

    # Load templates for all requested types
    rally_icons = {}
    for rt in rally_types:
        icon = get_template(f"elements/rally/{rt}.png")
        if icon is not None:
            rally_icons[rt] = icon
    join_btn = get_template("elements/rally/join.png")

    if not rally_icons or join_btn is None:
        log.warning("Missing rally images")
        return False

    types_str = "/".join(rally_types)

    def _backout_to_war_screen():
        """Back out of a rally detail / popup to the war screen list.
        Checks screen state between each step to avoid overshooting
        (e.g. backing out past td_screen into the quit dialog).
        Returns True if successfully back on war screen, False otherwise."""
        for attempt in range(3):
            # Try close button first
            tap_image("close_x.png", device)
            timed_wait(device, lambda: check_screen(device) == Screen.WAR,
                       3.0, "jr_backout_close_x")

            # Check where we are before continuing
            current = check_screen(device)
            if current == Screen.WAR:
                return True
            if current == Screen.TROOP_DETAIL:
                # Overshot past war_screen — navigate forward instead of back
                log.debug("Backed out to td_screen, re-entering war_screen")
                return navigate(Screen.WAR, device)

            # Still in popup — try back arrow
            tap_image("back_arrow.png", device, threshold=0.7)
            if _on_war_screen(device):
                return True

            log.debug("Back-out attempt %d — not on war screen yet", attempt + 1)
        log.error("Back-out failed after 3 attempts — stuck on %s", check_screen(device))
        save_failure_screenshot(device, "backout_stuck")
        return False

    def _exit_war_screen():
        """Navigate back from war screen to map."""
        navigate(Screen.MAP, device)

    # Keywords to verify rally type from OCR text on the war screen row
    _rally_verify_keywords = {
        QuestType.TITAN: ["titan"],
        QuestType.EVIL_GUARD: ["evil", "guard"],
        QuestType.PVP: ["pvp", "attack"],
        RallyType.CASTLE: ["castle"],
        RallyType.PASS: ["pass"],
        RallyType.TOWER: ["tower"],
    }

    def _ocr_label_at(screen, y_pos):
        """OCR the monster name label on the left side of the screen at a given Y position.
        The label is on a dark banner on the monster card. Returns lowercase text."""
        h, w = screen.shape[:2]
        # The label is on the left-side monster card (~first 300px wide).
        # It sits below the icon match area — extend generously downward.
        y_start = max(0, y_pos - 10)
        y_end = min(h, y_pos + 80)
        x_start = 0
        x_end = min(w, 300)
        label_crop = screen[y_start:y_end, x_start:x_end]
        gray = cv2.cvtColor(label_crop, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

        from vision import ocr_read
        results = ocr_read(gray, detail=0)
        return " ".join(results).lower()

    def _ocr_rally_owner(screen, join_y):
        """OCR the rally owner name from a war screen rally card.
        The name appears as "{Name}'s Troop" in the upper-right portion of the card,
        roughly 130-210px above the join/full button.
        Returns the owner name (without "'s Troop"), or a visual hash fallback
        like 'crop_a1b2c3d4' if OCR fails — never returns empty string."""
        h, w = screen.shape[:2]
        # The owner name is in the right section of the card, above the troop portraits.
        # join_y is the top-left Y of the join button template match.
        # Calibrated via live testing (Feb 2026): 130-210px above join, x:230-800.
        y_start = max(0, join_y - 210)
        y_end = max(0, join_y - 130)
        x_start = 230
        x_end = min(w, 800)
        if y_start >= y_end:
            return f"pos_{join_y}"
        owner_crop = screen[y_start:y_end, x_start:x_end]

        # Save debug crops so we can verify the region is correct
        _debug_dir = os.path.join(DEBUG_DIR, "owner_ocr")
        os.makedirs(_debug_dir, exist_ok=True)
        _crop_path = os.path.join(_debug_dir, f"owner_crop_y{join_y}.png")
        cv2.imwrite(_crop_path, owner_crop)

        gray = cv2.cvtColor(owner_crop, cv2.COLOR_BGR2GRAY)
        # Grayscale upscale (better accuracy than Otsu threshold on game UI text)
        upscaled = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)

        # Also save the preprocessed image for debugging
        cv2.imwrite(os.path.join(_debug_dir, f"owner_thresh_y{join_y}.png"), upscaled)

        from vision import ocr_read
        results = ocr_read(upscaled, detail=0)
        raw = " ".join(results).strip()

        if raw:
            log.info("Rally owner OCR (join_y=%d): '%s'", join_y, raw)
        else:
            log.warning("Rally owner OCR empty (join_y=%d, crop y:%d-%d x:%d-%d)",
                        join_y, y_start, y_end, x_start, x_end)

        # Extract owner name from "{Name}'s Troop" pattern.
        # OCR can mangle the apostrophe in various ways:
        #   "DNGs Troop"      — apostrophe dropped, s attached to name
        #   "DRP's Troop"     — clean read
        #   'Bchen" S Troop'  — smart quote artifact + uppercase S + space
        # Pattern allows optional whitespace/quotes between name and s/S.
        match = re.match(r"(.+?)[\s''\u2019\u201c\u201d\"]*[sS]\s+[Tt]roop", raw)
        if match:
            return match.group(1).strip()
        # Fallback: if OCR read something but didn't match pattern, return raw
        # (might still be useful for blacklisting)
        if raw:
            log.debug("Rally owner OCR raw: '%s' (no pattern match)", raw)
            return raw
        # OCR returned nothing — use visual hash of the crop as fallback ID
        # so blacklist tracking still works per unique rally card
        import hashlib
        crop_hash = hashlib.md5(owner_crop.tobytes()).hexdigest()[:8]
        log.debug("Rally owner OCR empty — using visual hash: crop_%s", crop_hash)
        return f"crop_{crop_hash}"

    def _text_matches_type(text, expected_type):
        """Check if OCR text contains keywords for the expected rally type."""
        keywords = _rally_verify_keywords.get(expected_type, [])
        return any(kw in text for kw in keywords)

    def check_for_joinable_rally():
        """Check current screen for a joinable rally of any requested type.
        Returns type string if joined, False if none found, 'lost' if off war screen.
        After a full-rally or slot-not-found, backs out and retries other visible
        rallies (up to 3 retries to avoid infinite loops on persistent failures)."""
        nonlocal pre_war_troops
        retries_left = 3
        screen = load_screenshot(device)
        if screen is None:
            return False

        while retries_left >= 0:
            if stop_check and stop_check():
                return False
            join_locs = find_all_matches(screen, "rally/join.png")
            if not join_locs:
                log.debug("No join buttons visible on war screen")
                return False

            # Pre-scan all icon matches and log counts
            icon_matches = {}
            for rt in rally_types:
                if rt in rally_icons:
                    icon_matches[rt] = find_all_matches(screen, f"rally/{rt}.png", threshold=0.9)
            counts_str = ", ".join(f"{rt}={len(locs)}" for rt, locs in icon_matches.items())
            log.debug("War screen scan: %d join buttons, icon matches: %s", len(join_locs), counts_str)

            should_rescan = False  # set True when we need fresh screenshot + rematch

            for rally_type in rally_types:
                if should_rescan:
                    break
                if rally_type not in rally_icons:
                    continue
                icon_h, icon_w = rally_icons[rally_type].shape[:2]
                rally_locs = icon_matches.get(rally_type, [])

                if not rally_locs:
                    continue  # No icon matches for this type — try next

                slot_found = False  # Track across rally_locs loop iterations
                for rally_x, rally_y in rally_locs:
                    # The monster name label is at the bottom of the card,
                    # approximately at rally_y + icon_h (just below the matched icon).
                    label_y = rally_y + icon_h

                    # Find the closest join button to this icon
                    best_join = None
                    best_dist = float('inf')
                    for join_x, join_y in join_locs:
                        dist = abs(join_y - label_y)
                        if dist < best_dist:
                            best_dist = dist
                            best_join = (join_x, join_y)

                    if best_join is None or best_dist > 120:
                        continue

                    join_x, join_y = best_join

                    # Verify the icon's label says the expected type
                    icon_label = _ocr_label_at(screen, label_y)
                    log.debug("Icon label OCR (y=%d): %s", label_y, icon_label)
                    if not _text_matches_type(icon_label, rally_type):
                        log.debug("Icon label mismatch — expected '%s', got: %s", rally_type, icon_label)
                        continue

                    # OCR the rally owner name and check against blacklist
                    rally_owner = _ocr_rally_owner(screen, join_y)
                    if rally_owner and _is_rally_owner_blacklisted(device, rally_owner):
                        log.info("Skipping %s rally by blacklisted owner '%s'", rally_type, rally_owner)
                        continue

                    log.info("Found joinable %s rally (icon_y=%d, join_y=%d, dist=%d, owner='%s')",
                             rally_type, label_y, join_y, best_dist, rally_owner or "unknown")

                    h, w = join_btn.shape[:2]
                    log.debug("Clicking join at (%d, %d)", join_x + w // 2, join_y + h // 2)
                    adb_tap(device, join_x + w // 2, join_y + h // 2)
                    timed_wait(device, lambda: False, 1, "jr_detail_load")

                    # Wait for rally detail screen to load — check for depart.png
                    # as the definitive signal, then look for slot or full indicators
                    slot_found = False
                    rally_full = False
                    detail_loaded = False
                    last_screen = None
                    start_time = time.time()
                    while time.time() - start_time < 6:
                        if stop_check and stop_check():
                            return False
                        s = load_screenshot(device)
                        if s is None:
                            time.sleep(0.5)
                            continue
                        last_screen = s

                        # Check for depart button — confirms detail screen loaded
                        if not detail_loaded and find_image(s, "depart.png", threshold=0.75):
                            detail_loaded = True

                        # Check for empty slot BEFORE full_rally — a rally can
                        # show full_rally.png while still having an open slot
                        match = find_image(s, "slot.png", threshold=0.8)
                        if match is None:
                            match = find_image(s, "slot.png", threshold=0.65)
                            if match:
                                log.debug("slot.png matched at lower threshold (%.0f%%)", get_last_best() * 100)
                        if match is None:
                            match = find_image(s, "slot.png", threshold=0.5)
                            if match:
                                log.debug("slot.png matched at 0.5 threshold (%.0f%%)", get_last_best() * 100)
                        if match:
                            max_val, max_loc, sh, sw = match
                            cx = max_loc[0] + sw // 2
                            cy = max_loc[1] + sh // 2
                            log.debug("Found slot at (%d, %d), confidence %.0f%%", cx, cy, max_val * 100)
                            adb_tap(device, cx, cy)
                            slot_found = True
                            break

                        # Only check full_rally after confirming no open slot
                        if find_image(s, "full_rally.png", threshold=0.8):
                            rally_full = True
                            break
                        time.sleep(0.5)

                    if rally_full:
                        log.warning("Rally is full — backing out to try others")
                        if not _backout_to_war_screen():
                            return "lost"
                        retries_left -= 1
                        should_rescan = True
                        break  # Break rally_locs loop, rescan in while loop

                    if not slot_found:
                        if last_screen is not None:
                            from navigation import _save_debug_screenshot
                            _save_debug_screenshot(device, "slot_not_found", last_screen)
                            best = get_last_best()
                            log.warning("No slot found (best match: %.0f%%, detail_loaded: %s) — backing out to try others",
                                        best * 100, detail_loaded)
                        else:
                            log.warning("No slot found (no screenshot captured) — backing out")
                        if not _backout_to_war_screen():
                            return "lost"
                        retries_left -= 1
                        should_rescan = True
                        break  # Break rally_locs loop, rescan in while loop

                    # Slot found — break rally_locs loop and proceed to depart
                    if slot_found:
                        break

                # After rally_locs loop: only attempt depart if we found a slot
                if should_rescan:
                    break  # Break rally_types loop → rescan check below

                if not slot_found:
                    continue  # No slot for this type → try next rally_type

                timed_wait(device, lambda: False, 1, "jr_slot_to_depart")
                # Capture which troop is selected before depart for slot tracking
                try:
                    portrait_result = capture_departing_portrait(device)
                    if portrait_result:
                        _last_depart_slot[device] = portrait_result[0]
                        log.debug("Departing troop: slot %d", portrait_result[0])
                except Exception:
                    log.debug("Portrait capture failed — proceeding without slot tracking")
                if tap_image("depart.png", device):
                    # Verify join succeeded — game should transition to map screen
                    timed_wait(device, lambda: check_screen(device) == Screen.MAP,
                               2, "jr_depart_to_map")
                    current_screen = check_screen(device)
                    if current_screen != Screen.MAP:
                        log.warning("After depart, expected map_screen but on %s — navigating to map", current_screen)
                        from navigation import _save_debug_screenshot
                        _save_debug_screenshot(device, "depart_wrong_screen")
                        navigate(Screen.MAP, device)

                    new_troops = troops_avail(device)
                    log.debug("Join verification: baseline=%d, post_depart=%d, screen=%s",
                              pre_war_troops, new_troops, current_screen)

                    if new_troops < pre_war_troops:
                        # Clear success: fewer troops than before
                        _clear_rally_owner_failures(device, rally_owner)
                        elapsed = time.time() - _jr_start
                        log.info("<<< join_rally: %s rally joined in %.1fs (troops %d -> %d)", rally_type, elapsed, pre_war_troops, new_troops)
                        stats.record_action(device, "join_rally", True, elapsed)
                        return rally_type
                    elif new_troops > pre_war_troops:
                        # Troop count went UP — disambiguate with panel status.
                        # If rallying count increased, join succeeded + a return
                        # completed simultaneously.  Otherwise, just a return.
                        post_snapshot = read_panel_statuses(device)
                        post_rallying = len(post_snapshot.troops_by_action(TroopAction.RALLYING)) if post_snapshot else 0
                        if post_rallying > pre_rallying:
                            # New rallying troop → join confirmed
                            _clear_rally_owner_failures(device, rally_owner)
                            elapsed = time.time() - _jr_start
                            log.info("<<< join_rally: %s rally joined in %.1fs "
                                     "(troops %d->%d, rallying %d->%d, return during join)",
                                     rally_type, elapsed, pre_war_troops, new_troops,
                                     pre_rallying, post_rallying)
                            stats.record_action(device, "join_rally", True, elapsed)
                            return rally_type
                        else:
                            # No new rallying → just a return, join failed
                            from navigation import _save_debug_screenshot
                            _save_debug_screenshot(device, "join_ambiguous_troops_up")
                            log.warning("Troops UP (%d->%d) but rallying unchanged (%d->%d) — join failed",
                                        pre_war_troops, new_troops, pre_rallying, post_rallying)
                            if rally_owner:
                                _record_rally_owner_failure(device, rally_owner)
                            pre_war_troops = new_troops
                            navigate(Screen.WAR, device)
                            continue  # Try next match
                    else:
                        # Same count — join likely failed
                        from navigation import _save_debug_screenshot
                        _save_debug_screenshot(device, "join_failed_after_depart")
                        log.warning("Depart clicked but troops unchanged (%d -> %d) — join failed. Screen: %s",
                                    pre_war_troops, new_troops, current_screen)

                        # Check for in-game error message on screen (e.g. "Cannot march
                        # across protected zones"). These flash briefly after failed actions.
                        error_screen = load_screenshot(device)
                        if error_screen is not None and rally_owner:
                            error_text = _ocr_error_banner(error_screen)
                            if error_text:
                                log.warning("In-game error detected: '%s' — blacklisting '%s' immediately",
                                            error_text, rally_owner)
                                _blacklist_rally_owner(device, rally_owner)
                            else:
                                _record_rally_owner_failure(device, rally_owner)
                        elif rally_owner:
                            _record_rally_owner_failure(device, rally_owner)

                        navigate(Screen.WAR, device)
                        continue  # Try next match
                else:
                    log.warning("Depart button not found — backing out")
                    if not _backout_to_war_screen():
                        return "lost"
                    return False

            if should_rescan:
                # Refresh screenshot and re-enter the while loop to find new matches
                screen = load_screenshot(device)
                if screen is None:
                    return False
                continue  # Retry with fresh screenshot

            # No more matches at this scroll position
            return False

    # Check current view first (may already see a joinable rally)
    result = check_for_joinable_rally()
    if result not in (False, "lost"):
        return result
    if result == "lost":
        log.warning("Lost war screen after failed join, aborting")
        return False

    # Check if we should scroll or not
    screen = load_screenshot(device)
    scroll_check = get_template("elements/scroll_or_not.png")

    should_skip_scroll = False
    if screen is not None and scroll_check is not None:
        res = cv2.matchTemplate(screen, scroll_check, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(res)
        should_skip_scroll = max_val > 0.8

    if should_skip_scroll:
        _exit_war_screen()
        return False

    # Scroll up to top (scroll position persists between visits)
    adb_swipe(device, 560, 300, 560, 1400, 500)
    timed_wait(device, lambda: False, 1.5, "jr_scroll_up_settle")

    # Scroll down and check 5 times
    for attempt in range(5):
        if not _on_war_screen(device):
            log.warning("No longer on war screen — aborting scroll loop")
            return False
        log.debug("Scroll down attempt %d/5", attempt + 1)
        adb_swipe(device, 560, 948, 560, 245, 500)
        timed_wait(device, lambda: False, 1.5, "jr_scroll_down_settle")
        result = check_for_joinable_rally()
        if result not in (False, "lost"):
            return result
        if result == "lost":
            log.warning("Lost war screen after failed join, aborting")
            return False

    # No rally found - exit war screen cleanly
    elapsed = time.time() - _jr_start
    log.info("<<< join_rally: no %s rally found after scrolling (%.1fs)", types_str, elapsed)
    _exit_war_screen()
    stats.record_action(device, "join_rally", False, elapsed)
    return False


@timed_action("join_war_rallies")
def join_war_rallies(device):
    """Try to join castle, pass, or tower rallies - checks all 3 on the same screenshot"""
    log = get_logger("actions", device)
    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    troops = troops_avail(device)
    if troops <= config.MIN_TROOPS_AVAILABLE:
        log.warning("Not enough troops available (have %d, need more than %d)", troops, config.MIN_TROOPS_AVAILABLE)
        return

    if not navigate(Screen.WAR, device):
        log.warning("Failed to navigate to war screen")
        return

    rally_types = [RallyType.CASTLE, RallyType.PASS, RallyType.TOWER]
    join_btn = get_template("elements/rally/join.png")
    if join_btn is None:
        log.warning("Missing join button image")
        return

    # Rally types we do NOT want to join
    exclude_types = [QuestType.TITAN, QuestType.EVIL_GUARD, RallyType.GROOT]

    def _backout_and_retry(reason):
        """Back out with (1010,285), verify we're still on war screen, then retry."""
        log.warning("%s — backing out", reason)
        adb_tap(device, 1010, 285)
        time.sleep(1)
        if not _on_war_screen(device):
            log.warning("No longer on war screen after backout — aborting")
            return False
        # Try 2 more times: check current view, then scroll down
        for retry in range(2):
            if check_all_rallies_on_screen():
                return True
            if not _on_war_screen(device):
                log.warning("No longer on war screen — aborting")
                return False
            adb_swipe(device, 560, 948, 560, 245, 500)
            time.sleep(1)
        return check_all_rallies_on_screen()

    def check_all_rallies_on_screen():
        """Check current screen for any joinable castle/pass/tower rally"""
        screen = load_screenshot(device)
        if screen is None:
            return False

        join_locs = find_all_matches(screen, "rally/join.png")
        if not join_locs:
            return False

        # Find all excluded rally icons so we can skip JOIN buttons near them
        excluded_ys = []
        for ex_type in exclude_types:
            for ex_x, ex_y in find_all_matches(screen, f"rally/{ex_type}.png"):
                excluded_ys.append(ex_y)

        for rally_type in rally_types:
            rally_locs = find_all_matches(screen, f"rally/{rally_type}.png")
            for rally_x, rally_y in rally_locs:
                for join_x, join_y in join_locs:
                    # Skip if this JOIN button is near an excluded rally type
                    if any(abs(join_y - ey) < 200 for ey in excluded_ys):
                        continue
                    if abs(join_y - rally_y) < 200:
                        log.info("Found joinable %s rally", rally_type)
                        h, w = join_btn.shape[:2]
                        adb_tap(device, join_x + w // 2, join_y + h // 2)

                        # Wait for an open slot, also check for titan error or full rally
                        slot_found = False
                        need_backout = False
                        backout_reason = None
                        last_screen = None
                        start_time = time.time()
                        while time.time() - start_time < 5:
                            s = load_screenshot(device)
                            if s is None:
                                time.sleep(0.5)
                                continue
                            last_screen = s
                            if find_image(s, "titanrally_error.png", threshold=0.8):
                                need_backout = True
                                backout_reason = "Titan rally detected"
                                break
                            if find_image(s, "full_rally.png", threshold=0.8):
                                need_backout = True
                                backout_reason = "Rally is full"
                                break
                            # Check for empty slot — try normal threshold, then lower
                            match = find_image(s, "slot.png", threshold=0.8)
                            if match is None:
                                match = find_image(s, "slot.png", threshold=0.65)
                                if match:
                                    log.debug("slot.png matched at lower threshold (%.0f%%)", get_last_best() * 100)
                            if match:
                                max_val, max_loc, sh, sw = match
                                cx = max_loc[0] + sw // 2
                                cy = max_loc[1] + sh // 2
                                log.debug("Found slot at (%d, %d), confidence %.0f%%", cx, cy, max_val * 100)
                                adb_tap(device, cx, cy)
                                slot_found = True
                                break
                            time.sleep(0.5)

                        if need_backout:
                            return _backout_and_retry(backout_reason)

                        if not slot_found:
                            # Save debug screenshot so we can see the rally detail screen
                            if last_screen is not None:
                                from navigation import _save_debug_screenshot
                                _save_debug_screenshot(device, "slot_not_found", last_screen)
                            return _backout_and_retry("No open slot found, rally may be full")

                        time.sleep(1)
                        if tap_image("depart.png", device):
                            log.info("Rally joined!")
                            return True
                        else:
                            return _backout_and_retry("Depart button not found")
        return False

    # Check current view first
    if check_all_rallies_on_screen():
        return

    # Check if we should scroll or not
    screen = load_screenshot(device)
    scroll_check = get_template("elements/scroll_or_not.png")

    should_skip_scroll = False
    if screen is not None and scroll_check is not None:
        result = cv2.matchTemplate(screen, scroll_check, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        should_skip_scroll = max_val > 0.8

    if should_skip_scroll:
        navigate(Screen.MAP, device)
        time.sleep(1)
        return

    # Scroll up to top
    adb_swipe(device, 560, 300, 560, 1400, 500)
    time.sleep(1.5)  # Wait for scroll momentum to settle

    if check_all_rallies_on_screen():
        return

    # Scroll down and check 5 times
    for attempt in range(5):
        if not _on_war_screen(device):
            log.warning("No longer on war screen — aborting scroll loop")
            return
        adb_swipe(device, 560, 948, 560, 245, 500)
        time.sleep(1.5)  # Wait for scroll momentum to settle
        if check_all_rallies_on_screen():
            return

    # Timed out - navigate back to map properly
    log.info("No war rallies available")
    navigate(Screen.MAP, device)
