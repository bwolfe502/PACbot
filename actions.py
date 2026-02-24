import cv2
import numpy as np
import time
import random
import os
import re
import subprocess

import config
from botlog import get_logger, timed_action, stats
from vision import (tap_image, wait_for_image_and_tap, load_screenshot,
                    find_image, find_all_matches, get_template,
                    adb_tap, adb_swipe, logged_tap, clear_click_trail,
                    save_failure_screenshot, read_ap, read_text)
from navigation import navigate, check_screen, DEBUG_DIR
from troops import troops_avail, all_troops_home, heal_all

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

_quest_rallies_pending = {}   # e.g. {"titan": 2, "eg": 1}
_quest_last_seen = {}         # e.g. {"titan": 10, "eg": 0} — last OCR counter values
_quest_pending_since = {}     # e.g. {"titan": 1708123456.0} — when pending was last increased

PENDING_TIMEOUT_S = 360       # 6 minutes — if counter hasn't advanced, assume rallies done/failed


def _track_quest_progress(quest_type, current):
    """Update pending rally count based on OCR counter progress.
    When the counter advances, we know some pending rallies completed."""
    last = _quest_last_seen.get(quest_type)
    if last is not None and current > last:
        completed = current - last
        pending = _quest_rallies_pending.get(quest_type, 0)
        _quest_rallies_pending[quest_type] = max(0, pending - completed)
        if completed > 0 and pending > 0:
            _log.debug("[%s] %d rally(s) completed, %d still pending", quest_type, completed, _quest_rallies_pending[quest_type])
        if _quest_rallies_pending.get(quest_type, 0) == 0:
            _quest_pending_since.pop(quest_type, None)
        else:
            _quest_pending_since[quest_type] = time.time()  # Reset timer for remaining
    elif last is not None and current < last:
        # Counter went backwards (quest reset / new day) — clear tracking
        _quest_rallies_pending[quest_type] = 0
        _quest_pending_since.pop(quest_type, None)
    _quest_last_seen[quest_type] = current

    # Timeout: if pending rallies haven't completed within PENDING_TIMEOUT_S, clear them
    if quest_type in _quest_pending_since:
        elapsed = time.time() - _quest_pending_since[quest_type]
        if elapsed > PENDING_TIMEOUT_S and _quest_rallies_pending.get(quest_type, 0) > 0:
            _log.warning("[%s] Pending rallies timed out after %.0fs — resetting", quest_type, elapsed)
            _quest_rallies_pending[quest_type] = 0
            _quest_pending_since.pop(quest_type, None)


def _record_rally_started(quest_type):
    """Record that we started/joined a rally for this quest type."""
    _quest_rallies_pending[quest_type] = _quest_rallies_pending.get(quest_type, 0) + 1
    # Only set timestamp on first pending (don't reset on subsequent)
    if quest_type not in _quest_pending_since:
        _quest_pending_since[quest_type] = time.time()
    _log.debug("[%s] Rally started — %d pending", quest_type, _quest_rallies_pending[quest_type])


def _effective_remaining(quest_type, current, target):
    """How many more rallies we actually need, accounting for in-progress ones."""
    base_remaining = target - current
    pending = _quest_rallies_pending.get(quest_type, 0)
    return max(0, base_remaining - pending)


def reset_quest_tracking():
    """Clear all rally tracking state. Call when auto quest starts or stops."""
    _quest_rallies_pending.clear()
    _quest_last_seen.clear()
    _quest_pending_since.clear()


# ---- Quest OCR helpers ----

def _classify_quest_text(text):
    """Classify quest type from OCR text."""
    t = text.lower()
    if "titan" in t:
        return "titan"
    if "evil" in t or "guard" in t:
        return "eg"
    if "pvp" in t or "attack" in t:
        return "pvp"
    if "gather" in t:
        return "gather"
    if "occupy" in t or "fortress" in t:
        return "fortress"
    if "tower" in t:
        return "tower"
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

    # Parse quest entries matching "Quest Name(X/Y)" pattern
    quests = []
    for match in re.finditer(r"(.+?)\((\d[\d,]*)/(\d[\d,]*)\)", raw_text):
        name = match.group(1).strip()
        current = int(match.group(2).replace(",", ""))
        target = int(match.group(3).replace(",", ""))
        quest_type = _classify_quest_text(name)

        # Override OCR targets with known minimum caps.
        # The game shows partial limits (e.g. "3/5" for titans) but the real daily
        # caps are higher. Trust OCR if it reads 15 or 20 for titans.
        ocr_target = target
        if quest_type == "titan" and target < 15:
            target = 15
        elif quest_type == "eg":
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
        skip = " (skip)" if quest_type in ("gather", "fortress", "tower", None) else ""
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
                joined = join_rally(["eg", "titan"], device)
            elif quest_img == "eg.png":
                log.info("Attempting to join an Evil Guard rally...")
                joined = join_rally("eg", device)
            else:
                log.info("Attempting to join a Titan rally...")
                joined = join_rally("titan", device)

            if not joined:
                if quest_img == "eg.png":
                    log.info("No rally to join, starting own EG rally")
                    if navigate("map_screen", device):
                        rally_eg(device)
                else:
                    log.info("No rally to join, starting own Titan rally")
                    if navigate("map_screen", device):
                        rally_titan(device)
            break
        elif quest_img == "pvp.png":
            if navigate("map_screen", device):
                target(device)
                if stop_check and stop_check():
                    return
                attack(device)
            break


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

    if not navigate("aq_screen", device):
        log.warning("Failed to navigate to quest screen")
        return False

    if stop_check and stop_check():
        return True

    # Claim rewards
    while tap_image("aq_claim.png", device):
        if stop_check and stop_check():
            return True
        time.sleep(1)

    if stop_check and stop_check():
        return True

    # Try OCR-based quest detection
    quests = _ocr_quest_rows(device)

    if quests is not None:
        # Deduplicate quest types — each rally counts toward ALL quests of the
        # same type (alliance + side), so we only need max(remaining) rallies.
        # e.g. titan 14/15 (alliance) + titan 0/5 (side) → keep 0/5, need 5 not 6.
        best_by_type = {}
        for q in quests:
            qt = q["quest_type"]
            remaining = q["target"] - q["current"]
            prev = best_by_type.get(qt)
            if prev is None or remaining > (prev["target"] - prev["current"]):
                best_by_type[qt] = q
        quests = list(best_by_type.values())

        # Update tracking with latest counter values
        for q in quests:
            if q["quest_type"] in ("titan", "eg", "pvp"):
                _track_quest_progress(q["quest_type"], q["current"])

        # Filter to quests that still need work (accounting for pending rallies)
        actionable = []
        for q in quests:
            if q["completed"] or q["quest_type"] not in ("titan", "eg", "pvp"):
                continue
            eff = _effective_remaining(q["quest_type"], q["current"], q["target"])
            if eff > 0:
                actionable.append(q)

        if not actionable:
            # Check if we're truly done or just waiting for pending rallies
            pending_types = [qt for qt, cnt in _quest_rallies_pending.items() if cnt > 0]
            if pending_types:
                pending_str = ", ".join(f"{qt} ({_quest_rallies_pending[qt]})" for qt in pending_types)
                log.info("Waiting for pending rallies to complete: %s", pending_str)
            else:
                log.info("No actionable quests remaining (all complete or skip-only)")
            return True

        remaining_str = ", ".join(
            f"{q['quest_type']} ({_effective_remaining(q['quest_type'], q['current'], q['target'])} needed)" for q in actionable
        )
        log.info("Actionable: %s", remaining_str)

        types_active = {q["quest_type"] for q in actionable}
        has_eg = "eg" in types_active
        has_titan = "titan" in types_active
        has_pvp = "pvp" in types_active

        # Priority: Join EG first > Join Titan > Start own Titan > Start own EG
        # Loop to join multiple rallies without re-checking the AQ screen each time.
        # Own rallies only run once per call (they're slower/more expensive).
        if has_eg or has_titan:
            # Build a quick lookup: quest_type -> (current, target) with most remaining
            quest_info = {}
            for q in actionable:
                qt = q["quest_type"]
                if qt in ("eg", "titan"):
                    existing = quest_info.get(qt)
                    if existing is None or (q["target"] - q["current"]) > (existing[1] - existing[0]):
                        quest_info[qt] = (q["current"], q["target"])

            # Heal once before the rally loop — titan/eg joins don't damage troops,
            # but a troop could be injured from previous activity.
            if config.AUTO_HEAL_ENABLED:
                heal_all(device)

            any_joined = False
            for attempt in range(15):  # safety limit
                if stop_check and stop_check():
                    return True

                # Check effective remaining for each type
                eg_needed = _effective_remaining("eg", *quest_info["eg"]) if "eg" in quest_info else 0
                titan_needed = _effective_remaining("titan", *quest_info["titan"]) if "titan" in quest_info else 0

                if eg_needed <= 0 and titan_needed <= 0:
                    log.info("All rally quests covered (pending completion)")
                    break

                # Build list of types to look for and join any in a single war screen pass
                types_to_join = []
                if eg_needed > 0:
                    types_to_join.append("eg")
                if titan_needed > 0:
                    types_to_join.append("titan")

                needed_str = ", ".join(f"{t} ({_effective_remaining(t, *quest_info[t])})" for t in types_to_join)
                log.info("Looking for %s rally (%s needed)...", "/".join(types_to_join), needed_str)
                joined_type = join_rally(types_to_join, device, skip_heal=True)
                if stop_check and stop_check():
                    return True

                if joined_type:
                    _record_rally_started(joined_type)
                    any_joined = True
                    continue  # Try to join another

                # No rally to join — start own rally, then loop to start more if needed
                started = False
                if titan_needed > 0:
                    log.info("No rally to join, starting own Titan rally")
                    if navigate("map_screen", device):
                        if rally_titan(device):
                            _record_rally_started("titan")
                            started = True
                    if stop_check and stop_check():
                        return True
                elif eg_needed > 0:
                    log.info("No rally to join, starting own EG rally")
                    if navigate("map_screen", device):
                        if rally_eg(device):
                            _record_rally_started("eg")
                            started = True
                    if stop_check and stop_check():
                        return True
                if not started:
                    break  # Rally failed, stop trying

            return True

        elif has_pvp:
            if navigate("map_screen", device):
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

    if check_screen(device) != "map_screen":
        log.debug("Not on map_screen, navigating...")
        if not navigate("map_screen", device):
            log.warning("Failed to navigate to map screen")
            return

    troops = troops_avail(device)

    if troops > config.MIN_TROOPS_AVAILABLE:
        logged_tap(device, 560, 675, "attack_selection")
        wait_for_image_and_tap("attack_button.png", device, timeout=5)
        time.sleep(1)
        tap_image("depart.png", device)
    else:
        log.warning("Not enough troops available (have %d, need more than %d)", troops, config.MIN_TROOPS_AVAILABLE)

@timed_action("reinforce_throne")
def reinforce_throne(device):
    """Heal all troops first (if auto heal enabled), then check troops and reinforce throne"""
    log = get_logger("actions", device)
    clear_click_trail()
    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    if check_screen(device) != "map_screen":
        log.debug("Not on map_screen, navigating...")
        if not navigate("map_screen", device):
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

    if check_screen(device) != "map_screen":
        log.debug("Not on map_screen, navigating...")
        if not navigate("map_screen", device):
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

    if check_screen(device) != "map_screen":
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

    log.warning("Teleport failed after %d attempts", attempt_count)
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
    if isinstance(rally_types, str):
        rally_types = [rally_types]

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

    if not navigate("war_screen", device):
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
            if current == "war_screen":
                return True
            if current == "td_screen":
                # Overshot past war_screen — navigate forward instead of back
                log.debug("Backed out to td_screen, re-entering war_screen")
                return navigate("war_screen", device)

            # Still in popup — try back button
            logged_tap(device, 75, 75, "jr_backout")
            time.sleep(0.5)
            if _on_war_screen(device):
                return True

            log.debug("Back-out attempt %d — not on war screen yet", attempt + 1)
        return False

    def _exit_war_screen():
        """Navigate back from war screen to map."""
        navigate("map_screen", device)

    # Keywords to verify rally type from OCR text on the war screen row
    _rally_verify_keywords = {
        "titan": ["titan"],
        "eg": ["evil", "guard"],
        "pvp": ["pvp", "attack"],
        "castle": ["castle"],
        "pass": ["pass"],
        "tower": ["tower"],
    }

    def _ocr_label_at(screen, y_pos, debug_name="rally_label_ocr"):
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

        # Save debug crop
        cv2.imwrite(os.path.join(DEBUG_DIR, f"{debug_name}.png"), gray)

        from vision import _get_ocr_reader
        reader = _get_ocr_reader()
        results = reader.readtext(gray, detail=0)
        return " ".join(results).lower()

    def _text_matches_type(text, expected_type):
        """Check if OCR text contains keywords for the expected rally type."""
        keywords = _rally_verify_keywords.get(expected_type, [])
        return any(kw in text for kw in keywords)

    def check_for_joinable_rally():
        """Check current screen for a joinable rally of any requested type.
        Returns type string if joined, False if none found, 'lost' if off war screen."""
        screen = load_screenshot(device)
        if screen is None:
            return False

        join_locs = find_all_matches(screen, "rally/join.png")
        if not join_locs:
            return False

        for rally_type in rally_types:
            if rally_type not in rally_icons:
                continue
            icon_h, icon_w = rally_icons[rally_type].shape[:2]
            rally_locs = find_all_matches(screen, f"rally/{rally_type}.png", threshold=0.9)

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
                icon_label = _ocr_label_at(screen, label_y, "rally_icon_label")
                log.debug("Icon label OCR (y=%d): %s", label_y, icon_label)
                if not _text_matches_type(icon_label, rally_type):
                    log.debug("Icon label mismatch — expected '%s', got: %s", rally_type, icon_label)
                    continue

                # Verify what the join button is actually on (label at join button's Y)
                join_label = _ocr_label_at(screen, join_y, "rally_join_target")
                log.debug("Join target OCR (y=%d): %s", join_y, join_label)
                if not _text_matches_type(join_label, rally_type):
                    log.debug("Join button is on '%s', not '%s' — skipping", join_label, rally_type)
                    continue

                log.info("Found joinable %s rally (icon_y=%d, join_y=%d, dist=%d)", rally_type, label_y, join_y, best_dist)

                h, w = join_btn.shape[:2]
                log.debug("Clicking join at (%d, %d)", join_x + w // 2, join_y + h // 2)
                adb_tap(device, join_x + w // 2, join_y + h // 2)
                time.sleep(1)

                # Wait for slot or full rally (single screenshot per iteration)
                slot_found = False
                rally_full = False
                last_screen = None
                start_time = time.time()
                while time.time() - start_time < 5:
                    s = load_screenshot(device)
                    if s is None:
                        time.sleep(0.5)
                        continue
                    last_screen = s

                    # Check full rally
                    if find_image(s, "full_rally.png", threshold=0.8):
                        rally_full = True
                        break

                    # Check for empty slot — try normal threshold, then lower
                    match = find_image(s, "slot.png", threshold=0.8)
                    if match is None:
                        match = find_image(s, "slot.png", threshold=0.65)
                        if match:
                            log.debug("slot.png matched at lower threshold (%.0f%%)", find_image.last_best * 100)
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
                    log.warning("Rally is full — backing out")
                    if not _backout_to_war_screen():
                        return "lost"
                    return False  # Force fresh screenshot on next call

                if not slot_found:
                    # Save debug screenshot so we can see the rally detail screen
                    if last_screen is not None:
                        from navigation import _save_debug_screenshot
                        _save_debug_screenshot(device, "slot_not_found", last_screen)
                        best = find_image.last_best
                        log.warning("No slot found (best slot match: %.0f%%, best full_rally: check debug screenshot) — backing out", best * 100)
                    else:
                        log.warning("No slot found (no screenshot captured) — backing out")
                    if not _backout_to_war_screen():
                        return "lost"
                    return False  # Force fresh screenshot on next call

                time.sleep(1)
                if tap_image("depart.png", device):
                    # Verify join succeeded — game should transition to map screen
                    time.sleep(2)
                    current_screen = check_screen(device)
                    if current_screen != "map_screen":
                        log.warning("After depart, expected map_screen but on %s — navigating to map", current_screen)
                        from navigation import _save_debug_screenshot
                        _save_debug_screenshot(device, "depart_wrong_screen")
                        navigate("map_screen", device)

                    new_troops = troops_avail(device)
                    log.debug("Join verification: baseline=%d, post_depart=%d, screen=%s",
                              pre_war_troops, new_troops, current_screen)

                    if new_troops < pre_war_troops:
                        # Clear success: fewer troops than before
                        log.info("%s rally joined! (troops %d -> %d)", rally_type, pre_war_troops, new_troops)
                        return rally_type
                    elif new_troops > pre_war_troops:
                        # Troop(s) returned during the join attempt — ambiguous.
                        # We sent 1 out but got back more than we lost. Join probably
                        # succeeded AND a previous rally completed simultaneously.
                        log.info("%s rally LIKELY joined (troops %d -> %d, troop(s) returned during join)",
                                 rally_type, pre_war_troops, new_troops)
                        # Do a second read after a short delay to check stability
                        time.sleep(1)
                        recheck_troops = troops_avail(device)
                        log.debug("Recheck troops after 1s: %d", recheck_troops)
                        from navigation import _save_debug_screenshot
                        _save_debug_screenshot(device, "join_ambiguous_troop_increase")
                        # Treat as success — we tapped depart and landed on map_screen
                        return rally_type
                    else:
                        # Same count — genuine failure (rally was full, depart didn't work)
                        from navigation import _save_debug_screenshot
                        _save_debug_screenshot(device, "join_failed_after_depart")
                        log.warning("Depart clicked but troops unchanged (%d -> %d) — join failed. Screen: %s",
                                    pre_war_troops, new_troops, current_screen)
                        navigate("war_screen", device)
                        continue  # Try next match
                else:
                    log.warning("Depart button not found — backing out")
                    if not _backout_to_war_screen():
                        return "lost"
                    return False

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
        adb_swipe(device, 560, 948, 560, 245, 500)
        time.sleep(1.5)  # Wait for scroll momentum to settle
        result = check_for_joinable_rally()
        if result not in (False, "lost"):
            return result
        if result == "lost":
            log.warning("Lost war screen after failed join, aborting")
            return False

    # No rally found - exit war screen cleanly
    log.info("No %s rally found after scrolling", types_str)
    _exit_war_screen()
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
    inverted = cv2.bitwise_not(gray)

    # Save debug crop so we can inspect what OCR sees
    cv2.imwrite(os.path.join(DEBUG_DIR, "ap_menu_crop.png"), inverted)
    log.debug("AP menu OCR: saved debug/ap_menu_crop.png (region %s)", _AP_MENU_REGION)

    from vision import _get_ocr_reader
    reader = _get_ocr_reader()
    results = reader.readtext(inverted, allowlist="0123456789/", detail=0)
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
    log.info("Attempting to restore AP (need %d)...", needed)

    # Navigate to map screen
    if not navigate("map_screen", device):
        log.warning("Failed to navigate to map screen for AP restore")
        return False

    # Tap SEARCH button to open the search/rally menu
    adb_tap(device, 900, 1800)
    time.sleep(1)

    # Tap the blue lightning bolt button (AP Recovery button in search menu)
    adb_tap(device, 315, 1380)
    time.sleep(1)

    # Wait for AP Recovery menu to appear (check for apwindow.png)
    menu_opened = False
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

    if not menu_opened:
        log.warning("AP Recovery menu did not open after 5 attempts")
        # Save screenshot for debugging
        if screen is not None:
            cv2.imwrite(os.path.join(DEBUG_DIR, "ap_menu_failed.png"), screen)
            log.debug("Saved debug/ap_menu_failed.png")
        # Try to close whatever is open
        _close_ap_menu(device)
        return False

    # Read current AP
    ap = _read_ap_from_menu(device)
    if ap is None:
        log.warning("Could not read AP from menu")
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
                    log.debug("%s AP potion had no effect (out of stock)", potion_labels[i])
                    break

    # Step 3: Try gem restore (50 AP per use, escalating gem cost, confirmation required)
    # When exhausted, button still shows 3500 but confirmation won't open.
    if config.AP_USE_GEMS and config.AP_GEM_LIMIT > 0 and current < needed:
        gems_spent = 0
        while current < needed:
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

    if current >= needed:
        log.info("AP restored successfully (%d >= %d)", current, needed)
        return True
    else:
        log.warning("Could not restore enough AP (%d < %d)", current, needed)
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

    if not navigate("map_screen", device):
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
        return False

def search_eg_reset(device):
    """Search for an Evil Guard without departing to reset titan distances.
    This brings nearby monsters closer again after repeated titan rallies."""
    log = get_logger("actions", device)
    log.info("Searching EG to reset titan distance...")

    if not navigate("map_screen", device):
        log.warning("Failed to navigate to map screen for EG reset")
        return False

    # Tap SEARCH button
    logged_tap(device, 900, 1800, "egreset_search_btn")
    time.sleep(1)

    # Tap RALLY tab
    logged_tap(device, 850, 560, "egreset_rally_tab")
    time.sleep(1)

    # Select Evil Guard
    if not wait_for_image_and_tap("rally_eg_select.png", device, timeout=5, threshold=0.65):
        log.warning("Failed to find Evil Guard select for reset")
        tap_image("close_x.png", device)
        return False
    time.sleep(1)

    # Tap Search to trigger the distance reset
    if not wait_for_image_and_tap("search.png", device, timeout=5, threshold=0.65):
        log.warning("Failed to find Search button for EG reset")
        tap_image("close_x.png", device)
        return False
    time.sleep(1)

    # Close out — tap X twice (EG view + search menu)
    tap_image("close_x.png", device)
    time.sleep(0.5)
    tap_image("close_x.png", device)
    time.sleep(0.5)

    log.info("EG search complete — titan distances reset")
    return True

@timed_action("rally_eg")
def rally_eg(device):
    """Start an evil guard rally attacking all 6 dark priests around an EG.

    Flow per priest: tap priest → check/proceed → depart → wait for return.
    Full failure detection with persistent screenshots at every failure point
    (saved to debug/failures/ — never auto-deleted between runs).
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

    if not navigate("map_screen", device):
        log.warning("Failed to navigate to map screen")
        return False

    # Open search menu → rally tab → select Evil Guard → search
    log.debug("EG rally: tapping search button")
    logged_tap(device, 900, 1800, "eg_search_btn")
    time.sleep(1)

    log.debug("EG rally: tapping rally tab")
    logged_tap(device, 850, 560, "eg_rally_tab")
    time.sleep(1)

    if not wait_for_image_and_tap("rally_eg_select.png", device, timeout=5, threshold=0.65):
        log.warning("EG rally: failed to find Evil Guard select")
        save_failure_screenshot(device, "eg_no_eg_select")
        logged_tap(device, 75, 75, "eg_close_menu")
        time.sleep(1)
        return False
    time.sleep(1)

    if not wait_for_image_and_tap("search.png", device, timeout=5, threshold=0.65):
        log.warning("EG rally: failed to find Search button")
        save_failure_screenshot(device, "eg_no_search_btn")
        logged_tap(device, 75, 75, "eg_close_menu")
        time.sleep(1)
        return False
    time.sleep(1)

    # Select EG boss on map
    log.debug("EG rally: tapping EG on map")
    logged_tap(device, 540, 665, "eg_boss_on_map")
    time.sleep(1)

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
        log.warning("P%d: check_and_proceed FAILED after 10 attempts", priest_num)
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
        """Tap the depart button with retries. Saves failure screenshot on exhaustion."""
        for attempt in range(5):
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
        log.warning("P%d: click_depart FAILED after 5 attempts", priest_num)
        save_failure_screenshot(device, f"eg_depart_fail_p{priest_num}")
        return False

    def wait_for_stationed(timeout_seconds, priest_num):
        """Wait for troops to return (stationed visible). Logs match location.
        Saves failure screenshot on timeout."""
        log.debug("P%d: waiting for stationed (timeout=%ds)...", priest_num, timeout_seconds)
        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            screen = load_screenshot(device)
            if stationed_img is not None and screen is not None:
                result = cv2.matchTemplate(screen, stationed_img, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
                if max_val > 0.8:
                    elapsed = time.time() - start_time
                    log.debug("P%d: stationed at (%d,%d) %.0f%% after %.1fs",
                              priest_num, max_loc[0], max_loc[1], max_val * 100, elapsed)
                    return True
            time.sleep(2)
        elapsed = time.time() - start_time
        log.warning("P%d: wait_for_stationed TIMED OUT after %.1fs", priest_num, elapsed)
        save_failure_screenshot(device, f"eg_stationed_timeout_p{priest_num}")
        return False

    def dismiss_and_verify_map(priest_num):
        """After a rally completes, dismiss any remaining dialog overlay and
        verify we're back on the map screen before tapping the next priest.

        Taps back button up to 3 times, checking after each whether the
        dialog is gone (no checked.png or depart.png visible).
        Saves failure screenshot if dialog can't be dismissed.
        """
        for attempt in range(3):
            log.debug("P%d: dismissing dialog (attempt %d/3)", priest_num, attempt + 1)
            logged_tap(device, 75, 75, f"eg_dismiss_p{priest_num}_a{attempt+1}")
            time.sleep(1.5)

            screen = load_screenshot(device)
            if screen is None:
                continue

            # If dialog elements are still visible, keep dismissing
            dialog_open = False
            if checked_img is not None:
                result = cv2.matchTemplate(screen, checked_img, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(result)
                if max_val > 0.8:
                    log.debug("P%d: dialog still open (checked %.0f%%), retrying",
                              priest_num, max_val * 100)
                    dialog_open = True

            if not dialog_open:
                depart_match = find_image(screen, "depart.png", threshold=0.8)
                if depart_match:
                    log.debug("P%d: dialog still open (depart visible), retrying", priest_num)
                    dialog_open = True

            if not dialog_open:
                log.debug("P%d: dialog dismissed, map screen ready", priest_num)
                return True

        log.warning("P%d: could not dismiss dialog after 3 attempts", priest_num)
        save_failure_screenshot(device, f"eg_dismiss_fail_p{priest_num}")
        return False

    # =====================================================
    # PRIEST 1 — first dark priest (already selected)
    # =====================================================
    log.info("P1: starting attack")

    if not check_and_proceed(1):
        return False
    if not click_depart_with_fallback(1):
        return False
    if not wait_for_stationed(240, 1):
        return False
    log.info("P1: rally completed")

    # =====================================================
    # PRIESTS 2–5 — loop over 4 dark priest positions
    # =====================================================
    priest_positions = [(172, 895), (259, 1213), (817, 1213), (929, 919)]
    for i, (x, y) in enumerate(priest_positions):
        pnum = i + 2
        log.info("P%d: starting attack at (%d, %d)", pnum, x, y)

        # Dismiss any remaining dialog from the previous rally
        if not dismiss_and_verify_map(pnum):
            return False

        # Tap the dark priest on the map
        logged_tap(device, x, y, f"eg_priest_{pnum}")
        time.sleep(1)

        if not check_and_proceed(pnum):
            return False
        try_stationed_before_depart(pnum)
        if not click_depart_with_fallback(pnum):
            return False
        time.sleep(1)
        if not wait_for_stationed(30, pnum):
            return False
        log.info("P%d: rally completed", pnum)

    # =====================================================
    # PRIEST 6 — final dark priest
    # =====================================================
    log.info("P6: starting final attack")

    # Dismiss dialog from priest 5
    if not dismiss_and_verify_map(6):
        return False

    logged_tap(device, 540, 913, "eg_final_priest")
    time.sleep(1)
    logged_tap(device, 421, 1412, "eg_final_attack")
    time.sleep(1)

    try_stationed_before_depart(6)
    if not click_depart_with_fallback(6):
        return False
    if not wait_for_stationed(240, 6):
        return False
    if not tap_image("stationed.png", device):
        log.warning("P6: final stationed tap failed")
        save_failure_screenshot(device, "eg_final_stationed_fail")
        return False

    time.sleep(2)
    if not tap_image("return.png", device):
        log.warning("EG rally: return button not found")
        save_failure_screenshot(device, "eg_return_fail")
        return False

    log.info("Evil Guard rally completed successfully — all 6 priests attacked!")
    return True

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

    if not navigate("war_screen", device):
        log.warning("Failed to navigate to war screen")
        return

    rally_types = ["castle", "pass", "tower"]
    join_btn = get_template("elements/rally/join.png")
    if join_btn is None:
        log.warning("Missing join button image")
        return

    # Rally types we do NOT want to join
    exclude_types = ["titan", "eg", "groot"]

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
                                    log.debug("slot.png matched at lower threshold (%.0f%%)", find_image.last_best * 100)
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
        navigate("map_screen", device)
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
    navigate("map_screen", device)
