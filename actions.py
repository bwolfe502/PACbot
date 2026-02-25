import cv2
import numpy as np
import time
import random
import os
import re
import subprocess

import config
from config import QuestType, RallyType, Screen
from botlog import get_logger, timed_action, stats
from vision import (tap_image, wait_for_image_and_tap, load_screenshot,
                    find_image, get_last_best, find_all_matches, get_template,
                    adb_tap, adb_swipe, logged_tap, clear_click_trail,
                    save_failure_screenshot, read_ap, read_text)
from navigation import navigate, check_screen, DEBUG_DIR
from troops import (troops_avail, all_troops_home, heal_all,
                    read_panel_statuses, TroopAction, capture_departing_portrait)

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

# ============================================================
# BASIC GAME ACTIONS
# ============================================================

# ---- Quest rally tracking ----
# Tracks rallies started but not yet reflected in the quest counter,
# so we don't over-rally while waiting for completion (1-5+ minutes each).
# Pending rallies auto-expire after PENDING_TIMEOUT_S to prevent getting stuck.

_quest_rallies_pending = {}   # e.g. {("127.0.0.1:5555", "titan"): 2}
_quest_last_seen = {}         # e.g. {("127.0.0.1:5555", "titan"): 10}
_quest_pending_since = {}     # e.g. {("127.0.0.1:5555", "titan"): 1708123456.0}

PENDING_TIMEOUT_S = config.QUEST_PENDING_TIMEOUT


def _track_quest_progress(device, quest_type, current):
    """Update pending rally count based on OCR counter progress.
    When the counter advances, we know some pending rallies completed."""
    key = (device, quest_type)
    last = _quest_last_seen.get(key)
    if last is not None and current > last:
        completed = current - last
        pending = _quest_rallies_pending.get(key, 0)
        _quest_rallies_pending[key] = max(0, pending - completed)
        if completed > 0:
            _log.info("[%s] %d rally(s) completed (%d->%d), %d still pending",
                      quest_type, completed, last, current, _quest_rallies_pending[key])
        if _quest_rallies_pending.get(key, 0) == 0:
            _quest_pending_since.pop(key, None)
        else:
            _quest_pending_since[key] = time.time()  # Reset timer for remaining
    elif last is not None and current < last:
        # Counter went backwards (quest reset / new day) — clear tracking
        _log.warning("[%s] Counter went backwards (%d->%d) — quest reset/new day, clearing tracking",
                     quest_type, last, current)
        _quest_rallies_pending[key] = 0
        _quest_pending_since.pop(key, None)
    _quest_last_seen[key] = current

    # Timeout: if pending rallies haven't completed within PENDING_TIMEOUT_S, clear them
    if key in _quest_pending_since:
        elapsed = time.time() - _quest_pending_since[key]
        if elapsed > PENDING_TIMEOUT_S and _quest_rallies_pending.get(key, 0) > 0:
            _log.warning("[%s] Pending rallies timed out after %.0fs — resetting", quest_type, elapsed)
            _quest_rallies_pending[key] = 0
            _quest_pending_since.pop(key, None)


def _record_rally_started(device, quest_type):
    """Record that we started/joined a rally for this quest type."""
    key = (device, quest_type)
    _quest_rallies_pending[key] = _quest_rallies_pending.get(key, 0) + 1
    # Only set timestamp on first pending (don't reset on subsequent)
    if key not in _quest_pending_since:
        _quest_pending_since[key] = time.time()
    _log.info("[%s] Rally started — %d pending", quest_type, _quest_rallies_pending[key])


def _effective_remaining(device, quest_type, current, target):
    """How many more rallies we actually need, accounting for in-progress ones."""
    key = (device, quest_type)
    base_remaining = target - current
    pending = _quest_rallies_pending.get(key, 0)
    return max(0, base_remaining - pending)


def reset_quest_tracking(device=None):
    """Clear rally tracking state. If device is given, clear only that device's state.
    If device is None, clear all state (backwards compatible)."""
    if device is None:
        _quest_rallies_pending.clear()
        _quest_last_seen.clear()
        _quest_pending_since.clear()
    else:
        for d in list(_quest_rallies_pending):
            if d[0] == device:
                del _quest_rallies_pending[d]
        for d in list(_quest_last_seen):
            if d[0] == device:
                del _quest_last_seen[d]
        for d in list(_quest_pending_since):
            if d[0] == device:
                del _quest_pending_since[d]


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

    from vision import _get_ocr_reader
    reader = _get_ocr_reader()
    results = reader.readtext(gray, detail=0)
    text = " ".join(results).strip().lower()
    if not text:
        return ""

    # Check if text matches known error patterns
    if any(kw in text for kw in _RALLY_ERROR_KEYWORDS):
        return text
    return ""


# ---- Quest OCR helpers ----

def _classify_quest_text(text):
    """Classify quest type from OCR text."""
    t = text.lower()
    if "titan" in t:
        return QuestType.TITAN
    if "evil" in t or "guard" in t:
        return QuestType.EVIL_GUARD
    if "pvp" in t or "attack" in t:
        return QuestType.PVP
    if "gather" in t:
        return QuestType.GATHER
    if "occupy" in t or "fortress" in t:
        return QuestType.FORTRESS
    if "tower" in t:
        return QuestType.TOWER
    return None


def _ocr_quest_rows(device):
    """Read quest counters from the AQ screen using OCR.
    Crops the quest list region, runs OCR, and parses counter patterns like 'Defeat Titans(0/5)'.
    Returns a list of quest dicts, or None if OCR fails.
    """
    log = get_logger("actions", device)
    screen = load_screenshot(device)
    if screen is None:
        return None

    # Crop quest list region — must reach all Side Quest entries below Alliance Quest
    quest_region = screen[590:1820, :]
    gray = cv2.cvtColor(quest_region, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

    # Save debug crop
    cv2.imwrite(os.path.join(DEBUG_DIR, "aq_ocr_crop.png"), gray)

    from vision import _get_ocr_reader
    reader = _get_ocr_reader()
    results = reader.readtext(gray, detail=0)
    raw_text = " ".join(results)
    log.debug("Quest OCR raw: %s", raw_text)

    if not raw_text.strip():
        log.warning("Quest OCR: no text detected")
        return None

    # Parse quest entries matching "Quest Name(X/Y)" pattern.
    # OCR often reads '0' as 'o'/'O', so accept those in the digit positions.
    quests = []
    for match in re.finditer(r"(.+?)\(([oO\d][\doO,]*)/([oO\d][\doO,]*)\)", raw_text):
        name = match.group(1).strip()
        raw_cur = match.group(2).replace(",", "")
        raw_tgt = match.group(3).replace(",", "")
        # Fix OCR o/O -> 0
        raw_cur = raw_cur.replace("o", "0").replace("O", "0")
        raw_tgt = raw_tgt.replace("o", "0").replace("O", "0")
        current = int(raw_cur)
        target = int(raw_tgt)
        quest_type = _classify_quest_text(name)

        # Override OCR targets with known minimum caps.
        # The game shows partial limits (e.g. "3/5" for titans) but the real daily
        # caps are higher. Trust OCR if it reads 15 or 20 for titans.
        ocr_target = target
        if quest_type == QuestType.TITAN and target < 15:
            target = 15
        elif quest_type == QuestType.EVIL_GUARD:
            target = 3

        completed = current >= target

        quest = {
            "quest_type": quest_type,
            "current": current,
            "target": target,
            "completed": completed,
            "text": match.group(0).strip(),
        }
        quests.append(quest)

        remaining = target - current
        status = "DONE" if completed else f"{remaining} remaining"
        skip = " (skip)" if quest_type in (QuestType.GATHER, QuestType.FORTRESS, QuestType.TOWER, None) else ""
        cap_note = f" (OCR showed {ocr_target}, cap overridden)" if target != ocr_target else ""
        log.debug("  %s: %d/%d — %s%s%s", quest_type or "unknown", current, target, status, skip, cap_note)

    if not quests:
        log.warning("Quest OCR: no quest patterns found in text")
        return None

    return quests


def _check_quests_legacy(device, stop_check):
    """Legacy PNG-based quest detection. Used as fallback when OCR fails."""
    log = get_logger("actions", device)
    screen = load_screenshot(device)
    if screen is None:
        log.warning("Failed to load screenshot for quest check")
        return

    # Check for quest types in lower segment of screen (below y=1280)
    lower_screen = screen[1280:, :]
    quest_images = ["eg.png", "titans.png", "pvp.png", "tower.png", "gold.png"]

    quest_scores = {}
    for quest_img in quest_images:
        button = get_template(f"elements/quests/{quest_img}")
        if button is None:
            continue
        result = cv2.matchTemplate(lower_screen, button, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        quest_scores[quest_img] = max_val

    score_str = " | ".join(f"{name}: {val*100:.0f}%" for name, val in
                           sorted(quest_scores.items(), key=lambda x: x[1], reverse=True))
    log.debug("Quest scores (legacy): %s", score_str)

    active_quests = [q for q, score in quest_scores.items() if score > 0.85]
    if active_quests:
        log.info("Active quests (legacy): %s", ", ".join(active_quests))

    has_eg = "eg.png" in active_quests
    has_titan = "titans.png" in active_quests

    for quest_img in active_quests:
        if stop_check and stop_check():
            return

        if quest_img in ("eg.png", "titans.png"):
            if has_eg and has_titan:
                log.info("Both EG and Titan quests active — joining any available rally...")
                joined = join_rally([QuestType.EVIL_GUARD, QuestType.TITAN], device)
            elif quest_img == "eg.png":
                log.info("Attempting to join an Evil Guard rally...")
                joined = join_rally(QuestType.EVIL_GUARD, device)
            else:
                log.info("Attempting to join a Titan rally...")
                joined = join_rally(QuestType.TITAN, device)

            if not joined:
                if quest_img == "eg.png":
                    if config.EG_RALLY_OWN_ENABLED:
                        log.info("No rally to join, starting own EG rally")
                        if navigate(Screen.MAP, device):
                            rally_eg(device, stop_check=stop_check)
                    else:
                        log.info("No EG rally to join — own rally disabled, skipping")
                else:
                    log.info("No rally to join, starting own Titan rally")
                    if navigate(Screen.MAP, device):
                        rally_titan(device)
            break
        elif quest_img == "pvp.png":
            if navigate(Screen.MAP, device):
                target(device)
                if stop_check and stop_check():
                    return
                attack(device)
            break


def _claim_quest_rewards(device, stop_check=None):
    """Tap the 'Claim' button on the quest screen until no more are found.
    Returns the number of rewards claimed, or -1 if stop_check triggered."""
    log = get_logger("actions", device)
    rewards_claimed = 0
    while tap_image("aq_claim.png", device):
        rewards_claimed += 1
        if stop_check and stop_check():
            return -1
        time.sleep(1)
    if rewards_claimed:
        log.info("Claimed %d quest reward(s)", rewards_claimed)
    return rewards_claimed


def _deduplicate_quests(quests):
    """Deduplicate quest list by type, keeping the entry with the most remaining.
    Each rally counts toward ALL quests of the same type (alliance + side),
    so we only need max(remaining) rallies per type.
    Returns a new list with at most one quest per quest_type."""
    best_by_type = {}
    for q in quests:
        qt = q["quest_type"]
        remaining = q["target"] - q["current"]
        prev = best_by_type.get(qt)
        if prev is None or remaining > (prev["target"] - prev["current"]):
            best_by_type[qt] = q
    return list(best_by_type.values())


def _get_actionable_quests(device, quests):
    """Filter quests to those that still need work (not complete, actionable type,
    and have effective remaining > 0 after accounting for pending rallies).
    Returns a list of quest dicts."""
    actionable = []
    for q in quests:
        if q["completed"] or q["quest_type"] not in (QuestType.TITAN, QuestType.EVIL_GUARD, QuestType.PVP):
            continue
        eff = _effective_remaining(device, q["quest_type"], q["current"], q["target"])
        if eff > 0:
            actionable.append(q)
    return actionable


def _run_rally_loop(device, actionable, stop_check=None):
    """Execute the rally join/start loop for EG and Titan quests.
    Tries to join rallies first, then starts own rallies if none found.
    Returns True if stop_check was triggered, False otherwise."""
    log = get_logger("actions", device)

    # Build a quick lookup: quest_type -> (current, target) with most remaining
    quest_info = {}
    for q in actionable:
        qt = q["quest_type"]
        if qt in (QuestType.EVIL_GUARD, QuestType.TITAN):
            existing = quest_info.get(qt)
            if existing is None or (q["target"] - q["current"]) > (existing[1] - existing[0]):
                quest_info[qt] = (q["current"], q["target"])

    # Heal once before the rally loop
    if config.AUTO_HEAL_ENABLED:
        log.debug("Healing before rally loop...")
        heal_all(device)

    for attempt in range(config.MAX_RALLY_ATTEMPTS):
        if stop_check and stop_check():
            return True

        # Check effective remaining for each type
        eg_needed = _effective_remaining(device, QuestType.EVIL_GUARD, *quest_info[QuestType.EVIL_GUARD]) if QuestType.EVIL_GUARD in quest_info else 0
        titan_needed = _effective_remaining(device, QuestType.TITAN, *quest_info[QuestType.TITAN]) if QuestType.TITAN in quest_info else 0

        if eg_needed <= 0 and titan_needed <= 0:
            log.info("All rally quests covered (pending completion)")
            break

        # Build list of types to look for
        types_to_join = []
        if eg_needed > 0:
            types_to_join.append(QuestType.EVIL_GUARD)
        if titan_needed > 0:
            types_to_join.append(QuestType.TITAN)

        needed_str = ", ".join(f"{t} ({_effective_remaining(device, t, *quest_info[t])})" for t in types_to_join)
        log.info("Looking for %s rally (%s needed)...", "/".join(types_to_join), needed_str)
        joined_type = join_rally(types_to_join, device, skip_heal=True)
        if stop_check and stop_check():
            return True

        if joined_type:
            _record_rally_started(device, joined_type)
            continue

        # No rally to join — start own rally
        started = False
        if titan_needed > 0:
            log.info("No rally to join, starting own Titan rally")
            if navigate(Screen.MAP, device):
                if rally_titan(device):
                    _record_rally_started(device, QuestType.TITAN)
                    started = True
            if stop_check and stop_check():
                return True
        elif eg_needed > 0:
            if config.EG_RALLY_OWN_ENABLED:
                log.info("No rally to join, starting own EG rally")
                if navigate(Screen.MAP, device):
                    if rally_eg(device, stop_check=stop_check):
                        _record_rally_started(device, QuestType.EVIL_GUARD)
                        started = True
            else:
                log.info("No EG rally to join — own rally disabled, skipping")
            if stop_check and stop_check():
                return True
        if not started:
            log.warning("Rally loop: could not join or start any rally — stopping")
            break

    return False


@timed_action("check_quests")
def check_quests(device, stop_check=None):
    """Check alliance/side quests using OCR counter reading, with PNG fallback.
    Reads quest counters (e.g. 'Defeat Titans(0/5)') to determine what needs work.
    Priority: Join EG > Join Titan > Start own Titan > Start own EG > PvP.
    stop_check: optional callable that returns True if we should abort immediately.
    """
    log = get_logger("actions", device)
    if stop_check and stop_check():
        return True

    if not navigate(Screen.ALLIANCE_QUEST, device):
        log.warning("Failed to navigate to quest screen")
        return False

    if stop_check and stop_check():
        return True

    if _claim_quest_rewards(device, stop_check) == -1:
        return True

    if stop_check and stop_check():
        return True

    # Try OCR-based quest detection
    quests = _ocr_quest_rows(device)

    if quests is not None:
        original_count = len(quests)
        quests = _deduplicate_quests(quests)
        if original_count != len(quests):
            log.debug("Quest dedup: %d raw -> %d unique types (kept max remaining per type)", original_count, len(quests))

        # Update tracking with latest counter values
        for q in quests:
            if q["quest_type"] in (QuestType.TITAN, QuestType.EVIL_GUARD, QuestType.PVP):
                _track_quest_progress(device, q["quest_type"], q["current"])

        actionable = _get_actionable_quests(device, quests)

        if not actionable:
            pending_types = [qt for (dev, qt), cnt in _quest_rallies_pending.items() if dev == device and cnt > 0]
            if pending_types:
                pending_str = ", ".join(f"{qt} ({_quest_rallies_pending[(device, qt)]})" for qt in pending_types)
                log.info("Waiting for pending rallies to complete: %s", pending_str)
            else:
                log.info("No actionable quests remaining (all complete or skip-only)")
            return True

        remaining_str = ", ".join(
            f"{q['quest_type']} ({_effective_remaining(device, q['quest_type'], q['current'], q['target'])} needed)" for q in actionable
        )
        log.info("Actionable: %s", remaining_str)

        types_active = {q["quest_type"] for q in actionable}
        has_eg = QuestType.EVIL_GUARD in types_active
        has_titan = QuestType.TITAN in types_active
        has_pvp = QuestType.PVP in types_active

        if has_eg or has_titan:
            if _run_rally_loop(device, actionable, stop_check):
                return True  # stop_check triggered
            return True

        elif has_pvp:
            if navigate(Screen.MAP, device):
                target(device)
                if stop_check and stop_check():
                    return True
                attack(device)
        return True
    else:
        # OCR failed — fall back to PNG matching
        log.warning("OCR failed, falling back to PNG quest detection")
        _check_quests_legacy(device, stop_check)
        return True

@timed_action("attack")
def attack(device):
    """Heal all troops first (if auto heal enabled), then check troops and attack"""
    log = get_logger("actions", device)
    clear_click_trail()
    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    if check_screen(device) != Screen.MAP:
        log.debug("Not on map_screen, navigating...")
        if not navigate(Screen.MAP, device):
            log.warning("Failed to navigate to map screen")
            return

    troops = troops_avail(device)

    if troops > config.MIN_TROOPS_AVAILABLE:
        logged_tap(device, 560, 675, "attack_selection")
        wait_for_image_and_tap("attack_button.png", device, timeout=5)
        time.sleep(1)  # Wait for attack dialog
        if tap_image("depart.png", device):
            log.info("Attack departed with %d troops available", troops)
        else:
            log.warning("Depart button not found after attack sequence")
    else:
        log.warning("Not enough troops available (have %d, need more than %d)", troops, config.MIN_TROOPS_AVAILABLE)

@timed_action("phantom_clash_attack")
def phantom_clash_attack(device):
    """Heal all troops first (if auto heal enabled), then attack in Phantom Clash mode"""
    log = get_logger("actions", device)
    clear_click_trail()
    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    # Determine if we need to attack based on troop statuses
    screen = load_screenshot(device)
    deployed = 5 - troops_avail(device)
    if deployed >= 5:
        # All troops out — check if any can attack (stationing or home)
        has_stationing = find_image(screen, "statuses/stationing.png") is not None
        has_battling = find_image(screen, "statuses/battling.png") is not None
        has_marching = find_image(screen, "statuses/marching.png") is not None
        has_returning = find_image(screen, "statuses/returning.png") is not None
        if not has_stationing:
            log.info("All 5 troops deployed and none stationing — skipping attack")
            return
        log.info("5 troops deployed but stationing troop found — proceeding to attack")
    else:
        log.info("%d/5 troops deployed — proceeding to attack", deployed)

    # Check for returning troops and drag to recall (skip if attack window already open)
    if not find_image(screen, "esb_middle_attack_window.png"):
        match = find_image(screen, "statuses/returning.png")
        if match:
            _, (mx, my), h, w = match
            cx, cy = mx + w // 2, my + h // 2
            log.info("Found returning troops at (%d, %d), dragging to (560, 1200)", cx, cy)
            adb_swipe(device, cx, cy, 560, 1200, duration_ms=500)
            time.sleep(1)

    logged_tap(device, 550, 450, "phantom_clash_attack_selection")

    # Wait for the attack menu to open (esb_middle_attack_window.png)
    start = time.time()
    menu_open = False
    while time.time() - start < 31:
        screen = load_screenshot(device)
        # Always check for attack button even while waiting for menu
        match = find_image(screen, "esb_attack.png")
        if match:
            _, (mx, my), h, w = match
            adb_tap(device, mx + w // 2, my + h // 2)
            log.debug("Tapped esb_attack.png")
            menu_open = True
            break
        if find_image(screen, "esb_middle_attack_window.png"):
            log.debug("Attack menu open, waiting for esb_attack.png...")
            menu_open = True
        else:
            log.debug("Attack menu not detected, retapping king")
            logged_tap(device, 550, 450, "phantom_clash_attack_selection")
        time.sleep(1)

    if not menu_open:
        log.warning("Timed out waiting for attack menu after 31s")
        return

    # Menu is open but attack button wasn't found yet — keep polling
    if not find_image(load_screenshot(device), "esb_attack.png"):
        while time.time() - start < 31:
            screen = load_screenshot(device)
            match = find_image(screen, "esb_attack.png")
            if match:
                _, (mx, my), h, w = match
                adb_tap(device, mx + w // 2, my + h // 2)
                log.debug("Tapped esb_attack.png")
                break
            time.sleep(1)
        else:
            log.warning("Timed out waiting for esb_attack.png after 31s")
            return

    time.sleep(1)  # Wait for attack dialog
    if tap_image("depart.png", device):
        log.info("Phantom Clash attack departed")
    else:
        log.warning("Depart button not found after Phantom Clash attack sequence")

@timed_action("reinforce_throne")
def reinforce_throne(device):
    """Heal all troops first (if auto heal enabled), then check troops and reinforce throne"""
    log = get_logger("actions", device)
    clear_click_trail()
    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    if check_screen(device) != Screen.MAP:
        log.debug("Not on map_screen, navigating...")
        if not navigate(Screen.MAP, device):
            log.warning("Failed to navigate to map screen")
            return

    troops = troops_avail(device)

    if troops > config.MIN_TROOPS_AVAILABLE:
        logged_tap(device, 560, 675, "throne_selection")
        wait_for_image_and_tap("throne_reinforce.png", device, timeout=5)
        time.sleep(1)
        tap_image("depart.png", device)
    else:
        log.warning("Not enough troops available (have %d, need more than %d)", troops, config.MIN_TROOPS_AVAILABLE)

@timed_action("target")
def target(device):
    """Open target menu, tap enemy tab, verify marker exists, then tap target.
    Returns True on success, False on general failure, 'no_marker' if target marker not found.
    """
    log = get_logger("actions", device)
    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    if check_screen(device) != Screen.MAP:
        log.debug("Not on map_screen, navigating...")
        if not navigate(Screen.MAP, device):
            log.warning("Failed to navigate to map screen")
            return False

    log.debug("Starting target sequence...")

    if not tap_image("target_menu.png", device):
        log.warning("Failed to find target_menu.png")
        return False
    time.sleep(1)

    # Tap the Enemy tab
    logged_tap(device, 740, 330, "target_enemy_tab")
    time.sleep(1)

    # Check that a target marker exists (retry up to 3 seconds)
    marker_found = False
    start_time = time.time()
    while time.time() - start_time < 3:
        screen = load_screenshot(device)
        if find_image(screen, "target_marker.png", threshold=0.7):
            marker_found = True
            break
        time.sleep(0.5)

    if not marker_found:
        log.warning("No target marker found!")
        return "no_marker"

    # Tap the target coordinates
    logged_tap(device, 350, 476, "target_coords")
    time.sleep(1)

    log.info("Target sequence complete!")
    return True

# ============================================================
# TELEPORT
# ============================================================

def _check_dead(screen, dead_img, device):
    """Check for dead.png on screen, click it if found. Returns True if dead was found."""
    log = get_logger("actions", device)
    if dead_img is None or screen is None:
        return False
    result = cv2.matchTemplate(screen, dead_img, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    if max_val > 0.95:
        log.warning("Found dead.png (confidence: %.1f%%), aborting teleport", max_val * 100)
        h, w = dead_img.shape[:2]
        logged_tap(device, max_loc[0] + w // 2, max_loc[1] + h // 2, "tp_dead_click")
        time.sleep(1)
        return True
    return False

def _find_green_pixel(screen, target_color, center_x=521, center_y=674, box_size=100, tolerance=20):
    """Check a region for green pixels using numpy vectorization. Returns True if found."""
    start_x = max(center_x - box_size // 2, 0)
    end_x = min(center_x + box_size // 2, screen.shape[1])
    start_y = max(center_y - box_size // 2, 0)
    end_y = min(center_y + box_size // 2, screen.shape[0])

    region = screen[start_y:end_y:5, start_x:end_x:5].astype(np.int16)
    diff = np.abs(region - np.array(target_color))
    matches = np.all(diff < tolerance, axis=2)
    return np.any(matches)

@timed_action("teleport")
def teleport(device):
    """Teleport to a location on the map"""
    log = get_logger("actions", device)
    log.debug("Checking if all troops are home before teleporting...")
    if not all_troops_home(device):
        log.warning("Troops are not home! Cannot teleport. Aborting.")
        return False

    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    if check_screen(device) != Screen.MAP:
        log.warning("Not on map_screen, can't teleport")
        return False

    log.debug("Starting teleport sequence...")

    logged_tap(device, 540, 960, "tp_start")
    time.sleep(2)

    # Load dead image once for reuse
    dead_img = get_template("elements/dead.png")

    # Check for dead before continuing
    screen = load_screenshot(device)
    if _check_dead(screen, dead_img, device):
        return False

    logged_tap(device, 540, 500, "tp_check")
    time.sleep(2)

    log.debug("Starting teleport search loop (90 second timeout)...")
    start_time = time.time()
    target_color = (0, 255, 0)  # BGR format for green
    attempt_count = 0
    max_attempts = 15

    while time.time() - start_time < 90 and attempt_count < max_attempts:
        attempt_count += 1
        log.debug("=== Teleport attempt #%d/%d ===", attempt_count, max_attempts)

        # Pan camera randomly
        log.debug("Panning camera randomly...")
        distance = random.randint(200, 400)
        direction = random.choice([-1, 1])
        end_x = max(100, min(980, 540 + distance * direction))

        adb_swipe(device, 540, 960, end_x, 960, 300)
        time.sleep(1)

        # Long press to search for random location
        log.debug("Long pressing to search for location...")
        adb_swipe(device, 540, 1400, 540, 1400, 1000)
        time.sleep(2)

        # Click the teleport/search button
        log.debug("Clicking teleport search button...")
        logged_tap(device, 780, 1400, "tp_search_btn")
        time.sleep(2)

        # Wait and check for green pixel
        green_check_start = time.time()
        found_green = False
        screen = None

        log.debug("Searching for green pixel (valid location)...")
        while time.time() - green_check_start < 3:
            screen = load_screenshot(device)
            if screen is None:
                time.sleep(1)
                continue

            if _check_dead(screen, dead_img, device):
                return False

            if _find_green_pixel(screen, target_color):
                found_green = True
                log.info("Green pixel found! Confirming teleport...")
                logged_tap(device, 760, 1700, "tp_confirm")
                time.sleep(2)
                log.info("Teleport complete!")
                return True

            time.sleep(1)

        if not found_green:
            log.debug("No valid location found (no green pixel). Canceling...")

            if screen is not None:
                match = find_image(screen, "cancel.png")
                if match:
                    _, max_loc, h, w = match
                    logged_tap(device, max_loc[0] + w // 2, max_loc[1] + h // 2, "tp_cancel")
                    log.debug("Clicked cancel button")
                else:
                    log.debug("Cancel button not found, waiting for UI to clear...")

            time.sleep(2)
            log.debug("Trying again...")

        elapsed = time.time() - start_time
        log.debug("Time elapsed: %.1fs / 90s", elapsed)

    log.error("Teleport failed after %d attempts", attempt_count)
    save_failure_screenshot(device, "teleport_timeout")
    return False

# ============================================================
# RALLY FUNCTIONS
# ============================================================

def join_rally(rally_types, device, skip_heal=False):
    """Join a rally of any given type(s) by looking for icons on the war screen.
    rally_types: string or list of strings (e.g. "eg" or ["eg", "titan"]).
    Checks all types simultaneously on each scroll position.
    skip_heal: if True, skip the heal_all call (caller already healed).
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
            time.sleep(0.5)

            # Check where we are before continuing
            current = check_screen(device)
            if current == Screen.WAR:
                return True
            if current == Screen.TROOP_DETAIL:
                # Overshot past war_screen — navigate forward instead of back
                log.debug("Backed out to td_screen, re-entering war_screen")
                return navigate(Screen.WAR, device)

            # Still in popup — try back button
            logged_tap(device, 75, 75, "jr_backout")
            time.sleep(0.5)
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

        from vision import _get_ocr_reader
        reader = _get_ocr_reader()
        results = reader.readtext(gray, detail=0)
        return " ".join(results).lower()

    def _ocr_rally_owner(screen, join_y):
        """OCR the rally owner name from a war screen rally card.
        The name appears as "{Name}'s Troop" in the upper-right portion of the card,
        roughly 100-160px above the join/full button.
        Returns the owner name (without "'s Troop"), or empty string on failure."""
        h, w = screen.shape[:2]
        # The owner name is in the right section of the card, above the troop portraits.
        # join_y is the top-left Y of the join button template match.
        y_start = max(0, join_y - 160)
        y_end = max(0, join_y - 80)
        x_start = 230
        x_end = min(w, 650)
        if y_start >= y_end:
            return ""
        owner_crop = screen[y_start:y_end, x_start:x_end]
        gray = cv2.cvtColor(owner_crop, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

        from vision import _get_ocr_reader
        reader = _get_ocr_reader()
        results = reader.readtext(gray, detail=0)
        raw = " ".join(results).strip()

        # Extract owner name from "{Name}'s Troop" pattern
        match = re.match(r"(.+?)[''\u2019]s\s+[Tt]roop", raw)
        if match:
            return match.group(1).strip()
        # Fallback: if OCR read something but didn't match pattern, return raw
        # (might still be useful for blacklisting)
        log.debug("Rally owner OCR raw: '%s' (no pattern match)", raw)
        return raw

    def _text_matches_type(text, expected_type):
        """Check if OCR text contains keywords for the expected rally type."""
        keywords = _rally_verify_keywords.get(expected_type, [])
        return any(kw in text for kw in keywords)

    def check_for_joinable_rally():
        """Check current screen for a joinable rally of any requested type.
        Returns type string if joined, False if none found, 'lost' if off war screen.
        After a full-rally or slot-not-found, backs out and retries other visible
        rallies (up to 3 retries to avoid infinite loops on persistent failures)."""
        retries_left = 3
        screen = load_screenshot(device)
        if screen is None:
            return False

        while retries_left >= 0:
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
                    time.sleep(1)

                    # Wait for rally detail screen to load — check for depart.png
                    # as the definitive signal, then look for slot or full indicators
                    slot_found = False
                    rally_full = False
                    detail_loaded = False
                    last_screen = None
                    start_time = time.time()
                    while time.time() - start_time < 6:
                        s = load_screenshot(device)
                        if s is None:
                            time.sleep(0.5)
                            continue
                        last_screen = s

                        # Check full rally first (appears quickly)
                        if find_image(s, "full_rally.png", threshold=0.8):
                            rally_full = True
                            break

                        # Check for depart button — confirms detail screen loaded
                        if not detail_loaded and find_image(s, "depart.png", threshold=0.8):
                            detail_loaded = True

                        # Check for empty slot at multiple thresholds
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

                time.sleep(1)
                if tap_image("depart.png", device):
                    # Verify join succeeded — game should transition to map screen
                    time.sleep(2)
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
                        # Troop(s) returned during the join attempt — ambiguous.
                        # We sent 1 out but got back more than we lost. Join probably
                        # succeeded AND a previous rally completed simultaneously.
                        _clear_rally_owner_failures(device, rally_owner)
                        elapsed = time.time() - _jr_start
                        log.info("<<< join_rally: %s rally LIKELY joined in %.1fs (troops %d -> %d, troop(s) returned during join)",
                                 rally_type, elapsed, pre_war_troops, new_troops)
                        stats.record_action(device, "join_rally", True, elapsed)
                        return rally_type
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

    # Check current view first
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

    # Scroll up to top
    adb_swipe(device, 560, 300, 560, 1400, 500)
    time.sleep(1.5)  # Wait for scroll momentum to settle

    result = check_for_joinable_rally()
    if result not in (False, "lost"):
        return result
    if result == "lost":
        log.warning("Lost war screen after failed join, aborting")
        return False

    # Scroll down and check 5 times
    for attempt in range(5):
        if not _on_war_screen(device):
            log.warning("No longer on war screen — aborting scroll loop")
            return False
        log.debug("Scroll down attempt %d/5", attempt + 1)
        adb_swipe(device, 560, 948, 560, 245, 500)
        time.sleep(1.5)  # Wait for scroll momentum to settle
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

def _close_ap_menu(device):
    """Close the AP Recovery menu and the search menu behind it."""
    tap_image("close_x.png", device)  # Close AP Recovery modal
    time.sleep(0.5)
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

    from vision import _get_ocr_reader
    reader = _get_ocr_reader()
    results = reader.readtext(thresh, allowlist="0123456789/", detail=0)
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
    from vision import _get_ocr_reader
    reader = _get_ocr_reader()
    results = reader.readtext(gray, detail=0)
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

    # Read current AP
    ap = _read_ap_from_menu(device)
    if ap is None:
        log.warning("Could not read AP from menu")
        save_failure_screenshot(device, "ap_read_failed")
        _close_ap_menu(device)
        return False

    current, maximum = ap
    log.info("Current AP: %d/%d", current, maximum)

    if current >= needed:
        log.info("Already have enough AP (%d >= %d)", current, needed)
        _close_ap_menu(device)
        return True

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

    # Close AP Recovery menu and search menu
    _close_ap_menu(device)

    elapsed = time.time() - _ap_start
    if current >= needed:
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

    # Tap SEARCH button to open rally menu
    logged_tap(device, 900, 1800, "titan_search_btn")
    time.sleep(1)

    # Tap RALLY tab (rightmost tab in the search menu)
    logged_tap(device, 850, 560, "titan_rally_tab")
    time.sleep(1)

    if not wait_for_image_and_tap("rally_titan_select.png", device, timeout=5, threshold=0.65):
        log.warning("Failed to find Titan select")
        return False
    time.sleep(1)

    if not wait_for_image_and_tap("search.png", device, timeout=5, threshold=0.65):
        log.warning("Failed to find Search button")
        return False
    time.sleep(1)

    # Select titan on map and confirm
    logged_tap(device, 540, 900, "titan_on_map")
    time.sleep(1)
    logged_tap(device, 420, 1400, "titan_confirm")
    time.sleep(1)

    if tap_image("depart.png", device):
        log.info("Titan rally started!")
        return True
    else:
        log.warning("Failed to find depart button")
        save_failure_screenshot(device, "titan_depart_fail")
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
    time.sleep(1)

    logged_tap(device, 850, 560, "eg_rally_tab")
    time.sleep(1)

    if not wait_for_image_and_tap("rally_eg_select.png", device, timeout=5, threshold=0.65):
        log.warning("Failed to find Evil Guard select")
        tap_image("close_x.png", device)
        return False
    time.sleep(1)

    if not wait_for_image_and_tap("search.png", device, timeout=5, threshold=0.65):
        log.warning("Failed to find Search button")
        tap_image("close_x.png", device)
        return False
    time.sleep(1)

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
    logged_tap(device, x, y, f"probe_{label}")
    time.sleep(1.5)

    # Poll for attack dialog indicators
    checked_tmpl = get_template("elements/checked.png")
    unchecked_tmpl = get_template("elements/unchecked.png")

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

    # Only dismiss if not on map screen (tapping 75,75 on map opens profile)
    if check_screen(device) != Screen.MAP:
        logged_tap(device, 75, 75, f"probe_{label}_dismiss")
        time.sleep(0.5)
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
    if not _search_eg_center(device):
        save_failure_screenshot(device, "eg_search_failed")
        return False

    # Wait for search overlay to fully close and camera to settle
    time.sleep(1.5)
    # Verify we're back on map_screen (search overlay dismissed)
    if check_screen(device) != Screen.MAP:
        log.debug("EG: search overlay may still be open, waiting...")
        time.sleep(1.5)
        if check_screen(device) != Screen.MAP:
            log.warning("EG: not on map_screen after search — recovering")
            if not navigate(Screen.MAP, device):
                return False

    # Tap EG boss on map to enter the priest view
    log.debug("EG rally: tapping EG on map")
    logged_tap(device, EG_PRIEST_POSITIONS[0][0], EG_PRIEST_POSITIONS[0][1], "eg_boss_on_map")
    time.sleep(1.5)

    # Pre-load templates used in inner loops
    checked_img = get_template("elements/checked.png")
    stationed_img = get_template("elements/stationed.png")

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
                    time.sleep(1)
                    return True
                else:
                    log.debug("P%d: check_and_proceed %d/10 — checked best %.0f%%, tapping unchecked",
                              priest_num, attempt + 1, max_val * 100)
                    tap_image("unchecked.png", device)
                    time.sleep(2)
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

    def click_depart_with_fallback(priest_num):
        """Tap the depart button with retries. Captures departing portrait on
        first attempt for troop identity tracking. Saves failure screenshot on exhaustion."""
        for attempt in range(5):
            if attempt == 0:
                # Capture which troop is being sent (portrait + slot) before departing
                result = capture_departing_portrait(device)
                if result:
                    slot_id, _ = result
                    log.debug("P%d: departing troop is slot %d", priest_num, slot_id)
            if tap_image("depart.png", device):
                log.debug("P%d: depart tapped (attempt %d)", priest_num, attempt + 1)
                return True
            if tap_image("defending.png", device):
                log.debug("P%d: found defending, retrying depart", priest_num)
                time.sleep(1)
                if tap_image("depart.png", device):
                    log.debug("P%d: depart tapped after defending", priest_num)
                    return True
            if attempt < 4:
                log.debug("P%d: depart not found, retry %d/5...", priest_num, attempt + 1)
                time.sleep(2)
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
            time.sleep(3)
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
                if find_image(screen, "depart.png", threshold=0.8):
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

            logged_tap(device, 75, 75, f"eg_dismiss_p{priest_num}_a{attempt+1}")
            time.sleep(1.5)

        # Last resort: navigate to map
        log.warning("P%d: could not dismiss after 5 attempts, navigating to map", priest_num)
        if navigate(Screen.MAP, device):
            return True
        save_failure_screenshot(device, f"eg_dismiss_fail_p{priest_num}")
        return False

    # =====================================================
    # PRIEST 1 — EG boss tap (probe verifies dialog opened)
    # =====================================================
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
        if not poll_troop_ready(240, 1):
            return False
        log.info("P1: rally completed")
        attacks_completed += 1
        priests_dead += 1
    else:
        log.warning("P1: PROBE MISS — no dialog after tapping EG boss")
        save_failure_screenshot(device, "probe_P1_MISS")
        priests_dead += 1   # priest is already dead (killed by others)
        if check_screen(device) != Screen.MAP:
            logged_tap(device, 75, 75, "probe_P1_dismiss")
            time.sleep(0.5)

    # =====================================================
    # PRIESTS 2–5 — probe each, collect misses for retry
    # =====================================================
    missed_priests = []
    for i in range(1, 5):  # EG_PRIEST_POSITIONS[1] through [4]
        pnum = i + 1
        x, y = EG_PRIEST_POSITIONS[i]
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
            time.sleep(1)
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
    # or other UI.  Re-center on the EG, apply a slow camera drag to
    # shift the priest toward screen center, then re-probe.
    if missed_priests:
        log.info("Retrying %d missed priest(s) with camera nudge...", len(missed_priests))

    for (i, pnum, x, y) in missed_priests:
        if stop_check and stop_check():
            log.info("Retry aborted (stop requested)")
            return False

        # Dismiss any open dialog before re-centering
        if attacks_completed > 0:
            dismiss_and_verify_map(pnum)

        # Re-center camera on EG via search
        if not _search_eg_center(device):
            log.warning("P%d retry: re-center failed, assuming dead", pnum)
            priests_dead += 1
            continue

        # Wait for search overlay to close, verify map screen
        time.sleep(1.5)
        if check_screen(device) != Screen.MAP:
            time.sleep(1.5)
            if check_screen(device) != Screen.MAP:
                if not navigate(Screen.MAP, device):
                    log.warning("P%d retry: can't reach map, assuming dead", pnum)
                    priests_dead += 1
                    continue

        # Calculate nudge to bring priest ~60% toward screen center
        center_x, center_y = 540, 960
        nudge_dx = int((center_x - x) * 0.6)
        nudge_dy = int((center_y - y) * 0.6)
        end_x = max(50, min(1030, center_x + nudge_dx))
        end_y = max(50, min(1870, center_y + nudge_dy))

        log.info("P%d retry: nudging camera by (%+d, %+d)", pnum, nudge_dx, nudge_dy)
        adb_swipe(device, center_x, center_y, end_x, end_y, 1000)
        time.sleep(0.5)

        # Probe at adjusted position
        adj_x = x + nudge_dx
        adj_y = y + nudge_dy
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
            time.sleep(1)
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
    log.info("P6: starting final EG rally at (%d,%d)", x6, y6)

    # If retries shifted the camera, re-center before P6
    if missed_priests:
        log.info("P6: re-centering camera after retry nudge(s)")
        if not _search_eg_center(device):
            log.warning("P6: re-center failed after retries")
            return False
        time.sleep(1.5)
        if check_screen(device) != Screen.MAP:
            time.sleep(1.5)
            if check_screen(device) != Screen.MAP:
                if not navigate(Screen.MAP, device):
                    return False

    # Dismiss dialog from previous priest
    if not dismiss_and_verify_map(6):
        return False

    # Ensure a troop is available for the EG rally — the P5 troop may still
    # be stationed at the last dark priest.  Wait up to 300s for it to return.
    if troops_avail(device) <= config.MIN_TROOPS_AVAILABLE:
        log.info("P6: no troop available yet — waiting for one to free up")
        wait_start = time.time()
        while time.time() - wait_start < 300:
            if stop_check and stop_check():
                log.info("P6: troop wait aborted (stop requested)")
                return False
            snapshot = read_panel_statuses(device)
            if snapshot:
                deployed = [t for t in snapshot.troops if not t.is_home]
                summary = ", ".join(t.action.value for t in deployed)
                log.debug("P6: waiting for troop — current: %s", summary)
            if troops_avail(device) > config.MIN_TROOPS_AVAILABLE:
                log.info("P6: troop now available — proceeding with EG rally")
                break
            time.sleep(3)
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
        time.sleep(1)
        logged_tap(device, 421, 1412, "eg_final_attack")
        time.sleep(1)

        # Verify the attack dialog appeared
        for _ in range(4):
            s = load_screenshot(device)
            if s is not None:
                if find_image(s, "depart.png", threshold=0.8):
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
            logged_tap(device, 75, 75, "eg_p6_dismiss_retry")
            time.sleep(1)

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

    time.sleep(2)
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


# ============================================================
# MITHRIL MINING
# ============================================================

# Fixed coordinates for the Advanced Mithril screen
_MITHRIL_SLOT_Y = 1760
_MITHRIL_SLOTS_X = [138, 339, 540, 741, 942]

_MITHRIL_MINES = [
    (240, 720),   # Mine 1
    (540, 820),   # Mine 2
    (850, 730),   # Mine 3
    (230, 1080),  # Mine 4
    (540, 1200),  # Mine 5
]


@timed_action("mine_mithril")
def mine_mithril(device):
    """Navigate to Advanced Mithril, recall all troops, redeploy to all 5 mines."""
    log = get_logger("actions", device)

    # Step 1: Navigate to kingdom_screen
    if not navigate(Screen.KINGDOM, device):
        log.warning("Failed to navigate to kingdom screen")
        return False

    # Step 2: Scroll kingdom screen to bottom (multiple swipes for reliability)
    for _ in range(3):
        adb_swipe(device, 540, 960, 540, 400, duration_ms=300)
        time.sleep(0.5)
    time.sleep(1)

    # Step 3: Tap Dimensional Tunnel
    logged_tap(device, 280, 880, "dimensional_tunnel")
    time.sleep(2)

    # Step 4: Tap Advanced Mithril (center of screen)
    logged_tap(device, 540, 960, "advanced_mithril")
    time.sleep(2)

    # Clear deploy timer — troops are about to be recalled
    config.MITHRIL_DEPLOY_TIME.pop(device, None)

    # Step 5: Recall occupied slots — troops are always left-aligned, so
    # returning slot 1 causes the rest to shift left.  Just tap slot 1
    # up to 5 times; stop early when it's empty.
    recalled_count = 0
    slot_x = _MITHRIL_SLOTS_X[0]
    for i in range(5):
        adb_tap(device, slot_x, _MITHRIL_SLOT_Y)
        time.sleep(1)
        if wait_for_image_and_tap("mithril_return.png", device, timeout=2, threshold=0.7):
            log.debug("Recall %d: RETURN found, recalled", i + 1)
            recalled_count += 1
            time.sleep(1.5)
        else:
            log.debug("Recall %d: slot empty, all troops recalled", i + 1)
            break

    if recalled_count > 0:
        log.info("Recalled %d troops from mithril mines", recalled_count)
        time.sleep(1)

    # Step 6: Deploy to all 5 mines
    deployed_count = 0
    for i, (mine_x, mine_y) in enumerate(_MITHRIL_MINES):
        log.debug("Deploying to mine %d at (%d, %d)", i + 1, mine_x, mine_y)
        adb_tap(device, mine_x, mine_y)  # Tap mine
        time.sleep(2)

        # Look for ATTACK button in the mine popup
        if not wait_for_image_and_tap("mithril_attack.png", device, timeout=2, threshold=0.7):
            log.warning("Mine %d: no ATTACK button (occupied or missed)", i + 1)
            save_failure_screenshot(device, f"mithril_no_attack_mine{i+1}")
            adb_tap(device, 900, 500)  # dismiss popup
            time.sleep(1)
            continue
        time.sleep(2)

        # Wait for troop selection screen and tap DEPART
        if wait_for_image_and_tap("mithril_depart.png", device, timeout=4, threshold=0.7):
            deployed_count += 1
            if deployed_count == 1:
                config.MITHRIL_DEPLOY_TIME[device] = time.time()
            time.sleep(2)  # Wait for deploy animation to return to overview
        else:
            log.warning("Mine %d: depart button not found after ATTACK", i + 1)
            save_failure_screenshot(device, f"mithril_depart_fail_mine{i+1}")
            adb_tap(device, 900, 500)  # dismiss overlay
            time.sleep(1)

    log.info("Deployed %d/%d troops to mithril mines", deployed_count, len(_MITHRIL_MINES))

    # Step 7: Navigate back to map screen
    adb_tap(device, 75, 75)  # Back from Advanced Mithril
    time.sleep(1)
    adb_tap(device, 75, 75)  # Back from Dimensional Treasure
    time.sleep(1)
    navigate(Screen.MAP, device)

    # Step 8: Record timestamp
    config.LAST_MITHRIL_TIME[device] = time.time()

    return deployed_count > 0


def mine_mithril_if_due(device):
    """Run mithril mining if enabled and interval has elapsed.

    Safe to call frequently — returns immediately if not due.
    Designed to be called from other auto task runners between action cycles.
    """
    if not config.MITHRIL_ENABLED:
        return
    last = config.LAST_MITHRIL_TIME.get(device, 0)
    elapsed = time.time() - last
    if elapsed < config.MITHRIL_INTERVAL * 60:
        return
    log = get_logger("actions", device)
    log.info("Mithril mining due (%.0f min since last) — running between actions",
             elapsed / 60)
    mine_mithril(device)
