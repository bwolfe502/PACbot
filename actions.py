import cv2
import numpy as np
import time
import random
import os
import re

import config
from vision import (tap_image, wait_for_image_and_tap, load_screenshot,
                    find_image, find_all_matches, get_template,
                    adb_tap, adb_swipe, logged_tap, clear_click_trail,
                    read_ap, read_text)
from navigation import navigate, check_screen
from troops import troops_avail, all_troops_home, heal_all

# ============================================================
# BASIC GAME ACTIONS
# ============================================================

# ---- Quest rally tracking ----
# Tracks rallies started but not yet reflected in the quest counter,
# so we don't over-rally while waiting for completion (1-5+ minutes each).

_quest_rallies_pending = {}   # e.g. {"titan": 2, "eg": 1}
_quest_last_seen = {}         # e.g. {"titan": 10, "eg": 0} — last OCR counter values


def _track_quest_progress(quest_type, current):
    """Update pending rally count based on OCR counter progress.
    When the counter advances, we know some pending rallies completed."""
    last = _quest_last_seen.get(quest_type)
    if last is not None and current > last:
        completed = current - last
        pending = _quest_rallies_pending.get(quest_type, 0)
        _quest_rallies_pending[quest_type] = max(0, pending - completed)
        if completed > 0 and pending > 0:
            print(f"  [{quest_type}] {completed} rally(s) completed, {_quest_rallies_pending[quest_type]} still pending")
    elif last is not None and current < last:
        # Counter went backwards (quest reset / new day) — clear tracking
        _quest_rallies_pending[quest_type] = 0
    _quest_last_seen[quest_type] = current


def _record_rally_started(quest_type):
    """Record that we started/joined a rally for this quest type."""
    _quest_rallies_pending[quest_type] = _quest_rallies_pending.get(quest_type, 0) + 1
    print(f"  [{quest_type}] Rally started — {_quest_rallies_pending[quest_type]} pending")


def _effective_remaining(quest_type, current, target):
    """How many more rallies we actually need, accounting for in-progress ones."""
    base_remaining = target - current
    pending = _quest_rallies_pending.get(quest_type, 0)
    return max(0, base_remaining - pending)


def reset_quest_tracking():
    """Clear all rally tracking state. Call when auto quest starts or stops."""
    _quest_rallies_pending.clear()
    _quest_last_seen.clear()


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
    screen = load_screenshot(device)
    if screen is None:
        return None

    # Crop quest list region (y=590 to y=1150, full width)
    quest_region = screen[590:1150, :]
    gray = cv2.cvtColor(quest_region, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

    # Save debug crop
    debug_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")
    os.makedirs(debug_dir, exist_ok=True)
    cv2.imwrite(os.path.join(debug_dir, "aq_ocr_crop.png"), gray)

    from vision import _get_ocr_reader
    reader = _get_ocr_reader()
    results = reader.readtext(gray, detail=0)
    raw_text = " ".join(results)
    print(f"[{device}] === Quest OCR raw: {raw_text}")

    if not raw_text.strip():
        print(f"[{device}] Quest OCR: no text detected")
        return None

    # Parse quest entries matching "Quest Name(X/Y)" pattern
    quests = []
    for match in re.finditer(r"(.+?)\((\d[\d,]*)/(\d[\d,]*)\)", raw_text):
        name = match.group(1).strip()
        current = int(match.group(2).replace(",", ""))
        target = int(match.group(3).replace(",", ""))
        quest_type = _classify_quest_text(name)
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
        print(f"[{device}]   {quest_type or 'unknown'}: {current}/{target} — {status}{skip}")

    if not quests:
        print(f"[{device}] Quest OCR: no quest patterns found in text")
        return None

    return quests


def _check_quests_legacy(device, stop_check):
    """Legacy PNG-based quest detection. Used as fallback when OCR fails."""
    screen = load_screenshot(device)
    if screen is None:
        print(f"[{device}] Failed to load screenshot for quest check")
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
    print(f"[{device}] Quest scores (legacy): {score_str}")

    active_quests = [q for q, score in quest_scores.items() if score > 0.85]
    if active_quests:
        print(f"[{device}] Active quests (legacy): {', '.join(active_quests)}")

    has_eg = "eg.png" in active_quests
    has_titan = "titans.png" in active_quests

    for quest_img in active_quests:
        if stop_check and stop_check():
            return

        if quest_img in ("eg.png", "titans.png"):
            if has_eg and has_titan:
                print(f"[{device}] Both EG and Titan quests active — joining any available rally...")
                joined = join_rally("eg", device) or join_rally("titan", device)
            elif quest_img == "eg.png":
                print(f"[{device}] Attempting to join an Evil Guard rally...")
                joined = join_rally("eg", device)
            else:
                print(f"[{device}] Attempting to join a Titan rally...")
                joined = join_rally("titan", device)

            if not joined:
                if quest_img == "eg.png":
                    print(f"[{device}] No rally to join, starting own EG rally")
                    if navigate("map_screen", device):
                        rally_eg(device)
                else:
                    print(f"[{device}] No rally to join, starting own Titan rally")
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


def check_quests(device, stop_check=None):
    """Check alliance/side quests using OCR counter reading, with PNG fallback.
    Reads quest counters (e.g. 'Defeat Titans(0/5)') to determine what needs work.
    Priority: Join EG > Join Titan > Start own Titan > Start own EG > PvP.
    stop_check: optional callable that returns True if we should abort immediately.
    """
    if stop_check and stop_check():
        return

    if not navigate("aq_screen", device):
        print(f"[{device}] Failed to navigate to quest screen")
        return

    if stop_check and stop_check():
        return

    # Claim rewards
    while tap_image("aq_claim.png", device):
        if stop_check and stop_check():
            return
        time.sleep(1)

    if stop_check and stop_check():
        return

    # Try OCR-based quest detection
    quests = _ocr_quest_rows(device)

    if quests is not None:
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
                print(f"[{device}] Waiting for pending rallies to complete: {pending_str}")
            else:
                print(f"[{device}] No actionable quests remaining (all complete or skip-only)")
            return

        remaining_str = ", ".join(
            f"{q['quest_type']} ({_effective_remaining(q['quest_type'], q['current'], q['target'])} needed)" for q in actionable
        )
        print(f"[{device}] Actionable: {remaining_str}")

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

            any_joined = False
            for attempt in range(15):  # safety limit
                if stop_check and stop_check():
                    return

                # Check effective remaining for each type
                eg_needed = _effective_remaining("eg", *quest_info["eg"]) if "eg" in quest_info else 0
                titan_needed = _effective_remaining("titan", *quest_info["titan"]) if "titan" in quest_info else 0

                if eg_needed <= 0 and titan_needed <= 0:
                    print(f"[{device}] All rally quests covered (pending completion)")
                    break

                joined = False
                joined_type = None

                if eg_needed > 0:
                    print(f"[{device}] Trying to join EG rally ({eg_needed} still needed)...")
                    if join_rally("eg", device):
                        joined = True
                        joined_type = "eg"
                    if stop_check and stop_check():
                        return

                if not joined and titan_needed > 0:
                    print(f"[{device}] Trying to join Titan rally ({titan_needed} still needed)...")
                    if join_rally("titan", device):
                        joined = True
                        joined_type = "titan"
                    if stop_check and stop_check():
                        return

                if joined:
                    _record_rally_started(joined_type)
                    any_joined = True
                    continue  # Try to join another

                # No rally to join — start own rally, then loop to start more if needed
                started = False
                if titan_needed > 0:
                    print(f"[{device}] No rally to join, starting own Titan rally")
                    if navigate("map_screen", device):
                        if rally_titan(device):
                            _record_rally_started("titan")
                            started = True
                    if stop_check and stop_check():
                        return
                elif eg_needed > 0:
                    print(f"[{device}] No rally to join, starting own EG rally")
                    if navigate("map_screen", device):
                        if rally_eg(device):
                            _record_rally_started("eg")
                            started = True
                    if stop_check and stop_check():
                        return
                if not started:
                    break  # Rally failed, stop trying

        elif has_pvp:
            if navigate("map_screen", device):
                target(device)
                if stop_check and stop_check():
                    return
                attack(device)
    else:
        # OCR failed — fall back to PNG matching
        print(f"[{device}] OCR failed, falling back to PNG quest detection")
        _check_quests_legacy(device, stop_check)

def attack(device):
    """Heal all troops first (if auto heal enabled), then check troops and attack"""
    clear_click_trail()
    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    if check_screen(device) != "map_screen":
        print(f"[{device}] Not on map_screen, navigating...")
        if not navigate("map_screen", device):
            print(f"[{device}] Failed to navigate to map screen")
            return

    troops = troops_avail(device)

    if troops > config.MIN_TROOPS_AVAILABLE:
        logged_tap(device, 560, 675, "attack_selection")
        wait_for_image_and_tap("attack_button.png", device, timeout=5)
        time.sleep(1)
        tap_image("depart.png", device)
    else:
        print(f"[{device}] Not enough troops available (have {troops}, need more than {config.MIN_TROOPS_AVAILABLE})")

def reinforce_throne(device):
    """Heal all troops first (if auto heal enabled), then check troops and reinforce throne"""
    clear_click_trail()
    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    if check_screen(device) != "map_screen":
        print(f"[{device}] Not on map_screen, navigating...")
        if not navigate("map_screen", device):
            print(f"[{device}] Failed to navigate to map screen")
            return

    troops = troops_avail(device)

    if troops > config.MIN_TROOPS_AVAILABLE:
        logged_tap(device, 560, 675, "throne_selection")
        wait_for_image_and_tap("throne_reinforce.png", device, timeout=5)
        time.sleep(1)
        tap_image("depart.png", device)
    else:
        print(f"[{device}] Not enough troops available (have {troops}, need more than {config.MIN_TROOPS_AVAILABLE})")

def target(device):
    """Open target menu, tap enemy tab, verify marker exists, then tap target.
    Returns True on success, False on general failure, 'no_marker' if target marker not found.
    """
    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    if check_screen(device) != "map_screen":
        print(f"[{device}] Not on map_screen, navigating...")
        if not navigate("map_screen", device):
            print(f"[{device}] Failed to navigate to map screen")
            return False

    print(f"[{device}] Starting target sequence...")

    if not tap_image("target_menu.png", device):
        print(f"[{device}] Failed to find target_menu.png")
        return False
    time.sleep(1)

    # Tap the Enemy tab
    adb_tap(device, 740, 330)
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
        print(f"[{device}] No target marker found!")
        return "no_marker"

    # Tap the target coordinates
    adb_tap(device, 350, 476)
    time.sleep(1)

    print(f"[{device}] Target sequence complete!")
    return True

# ============================================================
# TELEPORT
# ============================================================

def _check_dead(screen, dead_img, device):
    """Check for dead.png on screen, click it if found. Returns True if dead was found."""
    if dead_img is None or screen is None:
        return False
    result = cv2.matchTemplate(screen, dead_img, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    if max_val > 0.95:
        print(f"[{device}] Found dead.png (confidence: {max_val*100:.1f}%), aborting teleport")
        h, w = dead_img.shape[:2]
        adb_tap(device, max_loc[0] + w // 2, max_loc[1] + h // 2)
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

def teleport(device):
    """Teleport to a location on the map"""
    print(f"[{device}] Checking if all troops are home before teleporting...")
    if not all_troops_home(device):
        print(f"[{device}] Troops are not home! Cannot teleport. Aborting.")
        return False

    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    if check_screen(device) != "map_screen":
        print(f"[{device}] Not on map_screen, can't teleport")
        return False

    print(f"[{device}] Starting teleport sequence...")

    adb_tap(device, 540, 960)
    time.sleep(2)

    # Load dead image once for reuse
    dead_img = get_template("elements/dead.png")

    # Check for dead before continuing
    screen = load_screenshot(device)
    if _check_dead(screen, dead_img, device):
        return False

    adb_tap(device, 540, 500)
    time.sleep(2)

    print(f"[{device}] Starting teleport search loop (90 second timeout)...")
    start_time = time.time()
    target_color = (0, 255, 0)  # BGR format for green
    attempt_count = 0
    max_attempts = 15

    while time.time() - start_time < 90 and attempt_count < max_attempts:
        attempt_count += 1
        print(f"[{device}] === Teleport attempt #{attempt_count}/{max_attempts} ===")

        # Pan camera randomly
        print(f"[{device}] Panning camera randomly...")
        distance = random.randint(200, 400)
        direction = random.choice([-1, 1])
        end_x = max(100, min(980, 540 + distance * direction))

        adb_swipe(device, 540, 960, end_x, 960, 300)
        time.sleep(1)

        # Long press to search for random location
        print(f"[{device}] Long pressing to search for location...")
        adb_swipe(device, 540, 1400, 540, 1400, 1000)
        time.sleep(2)

        # Click the teleport/search button
        print(f"[{device}] Clicking teleport search button...")
        adb_tap(device, 780, 1400)
        time.sleep(2)

        # Wait and check for green pixel
        green_check_start = time.time()
        found_green = False
        screen = None

        print(f"[{device}] Searching for green pixel (valid location)...")
        while time.time() - green_check_start < 3:
            screen = load_screenshot(device)
            if screen is None:
                time.sleep(1)
                continue

            if _check_dead(screen, dead_img, device):
                return False

            if _find_green_pixel(screen, target_color):
                found_green = True
                print(f"[{device}] ✓ Green pixel found! Confirming teleport...")
                adb_tap(device, 760, 1700)
                time.sleep(2)
                print(f"[{device}] Teleport complete!")
                return True

            time.sleep(1)

        if not found_green:
            print(f"[{device}] No valid location found (no green pixel). Canceling...")

            if screen is not None:
                match = find_image(screen, "cancel.png")
                if match:
                    _, max_loc, h, w = match
                    adb_tap(device, max_loc[0] + w // 2, max_loc[1] + h // 2)
                    print(f"[{device}] Clicked cancel button")
                else:
                    print(f"[{device}] Cancel button not found, waiting for UI to clear...")

            time.sleep(2)
            print(f"[{device}] Trying again...")

        elapsed = time.time() - start_time
        print(f"[{device}] Time elapsed: {elapsed:.1f}s / 90s")

    print(f"[{device}] ✗ Teleport failed after {attempt_count} attempts")
    return False

# ============================================================
# RALLY FUNCTIONS
# ============================================================

def join_rally(rally_type, device):
    """Join a specific type of rally by looking for its icon"""
    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    troops = troops_avail(device)
    if troops <= config.MIN_TROOPS_AVAILABLE:
        print(f"[{device}] Not enough troops available (have {troops}, need more than {config.MIN_TROOPS_AVAILABLE})")
        return False

    if not navigate("war_screen", device):
        print(f"[{device}] Failed to navigate to war screen")
        return False

    rally_icon = get_template(f"elements/rally/{rally_type}.png")
    join_btn = get_template("elements/rally/join.png")

    if rally_icon is None or join_btn is None:
        print(f"[{device}] Missing rally images")
        return False

    def _on_war_screen():
        """Check if we're still on the war screen."""
        screen = load_screenshot(device)
        if screen is None:
            return False
        if find_image(screen, "war_screen.png", threshold=0.8):
            return True
        print(f"[{device}] Not on war screen anymore")
        return False

    def _exit_war_screen():
        """Navigate back from war screen to map, checking screen between taps."""
        for _ in range(4):
            adb_tap(device, 75, 75)
            time.sleep(1)
            if check_screen(device) == "map_screen":
                return
        # Last resort — try bottom nav
        adb_tap(device, 965, 1865)
        time.sleep(1)

    def check_for_joinable_rally():
        """Check current screen for a joinable rally.
        Returns True if joined, False if none found, 'lost' if off war screen."""
        screen = load_screenshot(device)
        if screen is None:
            return False

        rally_locs = find_all_matches(screen, f"rally/{rally_type}.png")
        join_locs = find_all_matches(screen, "rally/join.png")

        # Match rallies with JOIN buttons by Y proximity
        for rally_x, rally_y in rally_locs:
            for join_x, join_y in join_locs:
                if abs(join_y - rally_y) < 200:
                    print(f"[{device}] Found joinable {rally_type} rally")

                    h, w = join_btn.shape[:2]
                    adb_tap(device, join_x + w // 2, join_y + h // 2)

                    # Wait for slot or full rally
                    slot_found = False
                    rally_full = False
                    start_time = time.time()
                    while time.time() - start_time < 5:
                        s = load_screenshot(device)
                        if s is not None and find_image(s, "full_rally.png", threshold=0.8):
                            rally_full = True
                            break
                        if tap_image("slot.png", device):
                            slot_found = True
                            break
                        time.sleep(0.5)

                    if rally_full:
                        print(f"[{device}] Rally is full — backing out")
                        tap_image("close_x.png", device)
                        time.sleep(1)
                        adb_tap(device, 75, 75)
                        time.sleep(1)
                        # Verify we're still on war screen
                        if not _on_war_screen():
                            return "lost"
                        return False

                    if not slot_found:
                        print(f"[{device}] No slot found — backing out")
                        adb_tap(device, 75, 75)
                        time.sleep(1)
                        if not _on_war_screen():
                            return "lost"
                        return False

                    time.sleep(1)
                    tap_image("depart.png", device)
                    print(f"[{device}] Rally joined!")
                    return True

        return False

    # Check current view first
    result = check_for_joinable_rally()
    if result is True:
        return True
    if result == "lost":
        print(f"[{device}] Lost war screen after failed join, aborting")
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
    time.sleep(1)

    result = check_for_joinable_rally()
    if result is True:
        return True
    if result == "lost":
        print(f"[{device}] Lost war screen after failed join, aborting")
        return False

    # Scroll down and check 5 times
    for attempt in range(5):
        if not _on_war_screen():
            print(f"[{device}] No longer on war screen — aborting scroll loop")
            return False
        adb_swipe(device, 560, 948, 560, 245, 500)
        time.sleep(1)
        result = check_for_joinable_rally()
        if result is True:
            return True
        if result == "lost":
            print(f"[{device}] Lost war screen after failed join, aborting")
            return False

    # No rally found - exit war screen cleanly
    print(f"[{device}] No {rally_type} rally found after scrolling")
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
    import re
    screen = load_screenshot(device)
    if screen is None:
        print(f"[{device}] AP menu OCR: screenshot failed")
        return None

    x1, y1, x2, y2 = _AP_MENU_REGION
    img = screen[y1:y2, x1:x2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    inverted = cv2.bitwise_not(gray)

    # Save debug crop so we can inspect what OCR sees
    debug_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")
    os.makedirs(debug_dir, exist_ok=True)
    cv2.imwrite(os.path.join(debug_dir, "ap_menu_crop.png"), inverted)
    print(f"[{device}] AP menu OCR: saved debug/ap_menu_crop.png (region {_AP_MENU_REGION})")

    from vision import _get_ocr_reader
    reader = _get_ocr_reader()
    results = reader.readtext(inverted, allowlist="0123456789/", detail=0)
    raw = " ".join(results).strip()
    print(f"[{device}] AP menu OCR raw: '{raw}'")

    match = re.search(r"(\d+)/(\d+)", raw)
    if match:
        return int(match.group(1)), int(match.group(2))
    print(f"[{device}] AP menu OCR: no 'X/Y' pattern found in '{raw}'")
    return None

def _read_gem_cost(device):
    """Read the gem cost from the confirmation dialog ('Spend X Gem(s)?').
    Returns the gem cost as an integer, or None if unreadable."""
    import re
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
    print(f"[{device}] Gem confirmation OCR: '{raw}'")
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
    print(f"[{device}] Attempting to restore AP (need {needed})...")

    # Navigate to map screen
    if not navigate("map_screen", device):
        print(f"[{device}] Failed to navigate to map screen for AP restore")
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
                print(f"[{device}] AP Recovery menu detected (attempt {attempt + 1})")
                menu_opened = True
                break
            else:
                print(f"[{device}] Waiting for AP Recovery menu... (attempt {attempt + 1}/5)")
        time.sleep(1)

    if not menu_opened:
        print(f"[{device}] AP Recovery menu did not open after 5 attempts")
        # Save screenshot for debugging
        debug_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")
        os.makedirs(debug_dir, exist_ok=True)
        if screen is not None:
            cv2.imwrite(os.path.join(debug_dir, "ap_menu_failed.png"), screen)
            print(f"[{device}] Saved debug/ap_menu_failed.png")
        # Try to close whatever is open
        _close_ap_menu(device)
        return False

    # Read current AP
    ap = _read_ap_from_menu(device)
    if ap is None:
        print(f"[{device}] Could not read AP from menu")
        _close_ap_menu(device)
        return False

    current, maximum = ap
    print(f"[{device}] Current AP: {current}/{maximum}")

    if current >= needed:
        print(f"[{device}] Already have enough AP ({current} >= {needed})")
        _close_ap_menu(device)
        return True

    # Step 1: Try FREE restore (up to 2 attempts — 25 AP each, 2x daily max)
    if config.AP_USE_FREE and current < needed:
        for free_attempt in range(2):
            if current >= needed:
                break
            print(f"[{device}] Trying free AP restore (attempt {free_attempt + 1}/2)...")
            adb_tap(device, *_AP_FREE_OPEN)  # "OPEN" button
            time.sleep(1.5)

            new_ap = _read_ap_from_menu(device)
            if new_ap is None:
                print(f"[{device}] Could not re-read AP after free restore")
                break
            if new_ap[0] > current:
                print(f"[{device}] Free restore worked: {current} -> {new_ap[0]}")
                current = new_ap[0]
            else:
                print(f"[{device}] Free restore had no effect (exhausted)")
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
                print(f"[{device}] Trying {potion_labels[i]} AP potion (use {use + 1})...")
                adb_tap(device, px, py)
                time.sleep(1.5)

                new_ap = _read_ap_from_menu(device)
                if new_ap is None:
                    print(f"[{device}] Could not re-read AP after potion")
                    break
                if new_ap[0] > current:
                    print(f"[{device}] Potion worked: {current} -> {new_ap[0]}")
                    current = new_ap[0]
                else:
                    print(f"[{device}] {potion_labels[i]} AP potion had no effect (out of stock)")
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
                print(f"[{device}] Gem confirmation did not appear (exhausted or unreadable)")
                break

            if gems_spent + gem_cost > config.AP_GEM_LIMIT:
                print(f"[{device}] Gem cost {gem_cost} would exceed limit "
                      f"({gems_spent}+{gem_cost} > {config.AP_GEM_LIMIT}), cancelling")
                tap_image("close_x.png", device)  # Close confirmation
                time.sleep(0.5)
                break

            # Confirm the purchase
            print(f"[{device}] Confirming gem restore ({gem_cost} gems)...")
            adb_tap(device, *_AP_GEM_CONFIRM)
            time.sleep(1.5)
            gems_spent += gem_cost

            new_ap = _read_ap_from_menu(device)
            if new_ap is None:
                print(f"[{device}] Could not re-read AP after gem restore")
                break
            if new_ap[0] > current:
                print(f"[{device}] Gem restore worked: {current} -> {new_ap[0]} "
                      f"({gems_spent} total gems spent)")
                current = new_ap[0]
            else:
                print(f"[{device}] Gem restore had no effect (out of gems?)")
                break

    # Close AP Recovery menu and search menu
    _close_ap_menu(device)

    if current >= needed:
        print(f"[{device}] AP restored successfully ({current} >= {needed})")
        return True
    else:
        print(f"[{device}] Could not restore enough AP ({current} < {needed})")
        return False

def rally_titan(device):
    """Start a titan rally from map screen"""
    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    troops = troops_avail(device)
    if troops <= config.MIN_TROOPS_AVAILABLE:
        print(f"[{device}] Not enough troops available (have {troops}, need more than {config.MIN_TROOPS_AVAILABLE})")
        return False

    # AP check (if unreadable, proceed anyway — game handles low AP with its own prompt)
    ap = read_ap(device)
    if ap is not None and ap[0] < config.AP_COST_RALLY_TITAN:
        if config.AUTO_RESTORE_AP_ENABLED:
            if not restore_ap(device, config.AP_COST_RALLY_TITAN):
                print(f"[{device}] Could not restore enough AP for titan rally")
                return False
        else:
            print(f"[{device}] Not enough AP for titan rally (have {ap[0]}, need {config.AP_COST_RALLY_TITAN})")
            return False

    if not navigate("map_screen", device):
        print(f"[{device}] Failed to navigate to map screen")
        return False

    # Tap SEARCH button to open rally menu
    adb_tap(device, 900, 1800)
    time.sleep(1)

    # Tap RALLY tab (rightmost tab in the search menu)
    adb_tap(device, 850, 560)
    time.sleep(1)

    if not wait_for_image_and_tap("rally_titan_select.png", device, timeout=5, threshold=0.65):
        print(f"[{device}] Failed to find Titan select")
        return False
    time.sleep(1)

    if not wait_for_image_and_tap("search.png", device, timeout=5, threshold=0.65):
        print(f"[{device}] Failed to find Search button")
        return False
    time.sleep(1)

    # Select titan on map and confirm
    adb_tap(device, 540, 900)
    time.sleep(1)
    adb_tap(device, 420, 1400)
    time.sleep(1)

    if tap_image("depart.png", device):
        print(f"[{device}] Titan rally started!")
        return True
    else:
        print(f"[{device}] Failed to find depart button")
        return False

def search_eg_reset(device):
    """Search for an Evil Guard without departing to reset titan distances.
    This brings nearby monsters closer again after repeated titan rallies."""
    print(f"[{device}] Searching EG to reset titan distance...")

    if not navigate("map_screen", device):
        print(f"[{device}] Failed to navigate to map screen for EG reset")
        return False

    # Tap SEARCH button
    adb_tap(device, 900, 1800)
    time.sleep(1)

    # Tap RALLY tab
    adb_tap(device, 850, 560)
    time.sleep(1)

    # Select Evil Guard
    if not wait_for_image_and_tap("rally_eg_select.png", device, timeout=5, threshold=0.65):
        print(f"[{device}] Failed to find Evil Guard select for reset")
        tap_image("close_x.png", device)
        return False
    time.sleep(1)

    # Tap Search to trigger the distance reset
    if not wait_for_image_and_tap("search.png", device, timeout=5, threshold=0.65):
        print(f"[{device}] Failed to find Search button for EG reset")
        tap_image("close_x.png", device)
        return False
    time.sleep(1)

    # Close out — tap X twice (EG view + search menu)
    tap_image("close_x.png", device)
    time.sleep(0.5)
    tap_image("close_x.png", device)
    time.sleep(0.5)

    print(f"[{device}] EG search complete — titan distances reset")
    return True

def rally_eg(device):
    """Start an evil guard rally from map screen"""
    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    troops = troops_avail(device)
    if troops <= config.MIN_TROOPS_AVAILABLE:
        print(f"[{device}] Not enough troops available (have {troops}, need more than {config.MIN_TROOPS_AVAILABLE})")
        return False

    # AP check (if unreadable, proceed anyway — game handles low AP with its own prompt)
    ap = read_ap(device)
    if ap is not None and ap[0] < config.AP_COST_EVIL_GUARD:
        if config.AUTO_RESTORE_AP_ENABLED:
            if not restore_ap(device, config.AP_COST_EVIL_GUARD):
                print(f"[{device}] Could not restore enough AP for evil guard rally")
                return False
        else:
            print(f"[{device}] Not enough AP for evil guard rally (have {ap[0]}, need {config.AP_COST_EVIL_GUARD})")
            return False

    if not navigate("map_screen", device):
        print(f"[{device}] Failed to navigate to map screen")
        return False

    # Tap SEARCH button to open rally menu
    adb_tap(device, 900, 1800)
    time.sleep(1)

    # Tap RALLY tab (rightmost tab in the search menu)
    adb_tap(device, 850, 560)
    time.sleep(1)

    if not wait_for_image_and_tap("rally_eg_select.png", device, timeout=5, threshold=0.65):
        print(f"[{device}] Failed to find Evil Guard select")
        return False
    time.sleep(1)

    if not wait_for_image_and_tap("search.png", device, timeout=5, threshold=0.65):
        print(f"[{device}] Failed to find Search button")
        return False
    time.sleep(1)

    # Select EG boss on map (no template yet - on-map position)
    adb_tap(device, 540, 665)
    time.sleep(1)

    # Pre-load templates used in inner loops
    checked_img = get_template("elements/checked.png")
    stationed_img = get_template("elements/stationed.png")

    def check_and_proceed():
        for attempt in range(10):
            screen = load_screenshot(device)
            if checked_img is not None and screen is not None:
                result = cv2.matchTemplate(screen, checked_img, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(result)

                if max_val > 0.8:
                    adb_tap(device, 540, 1500)
                    time.sleep(1)
                    return True
                else:
                    print(f"[{device}] check_and_proceed attempt {attempt+1}/10 — checked not found, tapping unchecked")
                    tap_image("unchecked.png", device)
                    time.sleep(2)
        print(f"[{device}] check_and_proceed FAILED after 10 attempts")
        return False

    def try_stationed_before_depart():
        start_time = time.time()
        while time.time() - start_time < 3:
            if tap_image("stationed.png", device):
                return True
            time.sleep(0.5)
        return False

    def click_depart_with_fallback():
        for attempt in range(5):
            if tap_image("depart.png", device):
                return True
            if tap_image("defending.png", device):
                time.sleep(1)
                if tap_image("depart.png", device):
                    return True
            if attempt < 4:
                print(f"[{device}] depart not found, retry {attempt+1}/5...")
                time.sleep(2)
        print(f"[{device}] click_depart_with_fallback FAILED after 5 attempts")
        return False

    def wait_for_stationed(timeout_seconds):
        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            screen = load_screenshot(device)
            if stationed_img is not None and screen is not None:
                result = cv2.matchTemplate(screen, stationed_img, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(result)
                if max_val > 0.8:
                    return True
            time.sleep(2)
        return False

    # First checkpoint
    if not check_and_proceed():
        return False
    if not click_depart_with_fallback():
        return False
    if not wait_for_stationed(240):
        return False

    # Repeat for 4 dark priests
    for x, y in [(172, 895), (259, 1213), (817, 1213), (929, 919)]:
        adb_tap(device, x, y)
        time.sleep(1)

        if not check_and_proceed():
            return False
        try_stationed_before_depart()
        if not click_depart_with_fallback():
            return False
        time.sleep(1)
        if not wait_for_stationed(30):
            return False

    # Final sequence
    adb_tap(device, 540, 913)
    time.sleep(1)
    adb_tap(device, 421, 1412)
    time.sleep(1)

    try_stationed_before_depart()
    if not click_depart_with_fallback():
        return False
    if not wait_for_stationed(240):
        return False
    if not tap_image("stationed.png", device):
        return False

    time.sleep(2)
    if not tap_image("return.png", device):
        return False

    print(f"[{device}] Evil Guard rally completed successfully!")
    return True

def join_war_rallies(device):
    """Try to join castle, pass, or tower rallies - checks all 3 on the same screenshot"""
    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    troops = troops_avail(device)
    if troops <= config.MIN_TROOPS_AVAILABLE:
        print(f"[{device}] Not enough troops available (have {troops}, need more than {config.MIN_TROOPS_AVAILABLE})")
        return

    if not navigate("war_screen", device):
        print(f"[{device}] Failed to navigate to war screen")
        return

    rally_types = ["castle", "pass", "tower"]
    join_btn = get_template("elements/rally/join.png")
    if join_btn is None:
        print(f"[{device}] Missing join button image")
        return

    # Rally types we do NOT want to join
    exclude_types = ["titan", "eg", "groot"]

    def _on_war_screen():
        """Check if we're still on the war screen by looking for war_screen.png."""
        screen = load_screenshot(device)
        if screen is None:
            return False
        if find_image(screen, "war_screen.png", threshold=0.8):
            return True
        print(f"[{device}] war_screen.png not found — not on war screen")
        return False

    def _backout_and_retry(reason):
        """Back out with (1010,285), verify we're still on war screen, then retry."""
        print(f"[{device}] {reason} — backing out")
        adb_tap(device, 1010, 285)
        time.sleep(1)
        if not _on_war_screen():
            print(f"[{device}] No longer on war screen after backout — aborting")
            return False
        # Try 2 more times: check current view, then scroll down
        for retry in range(2):
            if check_all_rallies_on_screen():
                return True
            if not _on_war_screen():
                print(f"[{device}] No longer on war screen — aborting")
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
                        print(f"[{device}] Found joinable {rally_type} rally")
                        h, w = join_btn.shape[:2]
                        adb_tap(device, join_x + w // 2, join_y + h // 2)

                        # Wait for an open slot, also check for titan error or full rally
                        slot_found = False
                        need_backout = False
                        backout_reason = None
                        start_time = time.time()
                        while time.time() - start_time < 5:
                            s = load_screenshot(device)
                            if s is None:
                                time.sleep(0.5)
                                continue
                            if find_image(s, "titanrally_error.png", threshold=0.8):
                                need_backout = True
                                backout_reason = "Titan rally detected"
                                break
                            if find_image(s, "full_rally.png", threshold=0.8):
                                need_backout = True
                                backout_reason = "Rally is full"
                                break
                            if find_image(s, "slot.png", threshold=0.8):
                                tap_image("slot.png", device)
                                slot_found = True
                                break
                            time.sleep(0.5)

                        if need_backout:
                            return _backout_and_retry(backout_reason)

                        if not slot_found:
                            return _backout_and_retry("No open slot found, rally may be full")

                        time.sleep(1)
                        if tap_image("depart.png", device):
                            print(f"[{device}] Rally joined!")
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
        for _ in range(4):
            adb_tap(device, 75, 75)
            time.sleep(1)
            if check_screen(device) == "map_screen":
                return
        adb_tap(device, 965, 1865)
        time.sleep(1)
        return

    # Scroll up to top
    adb_swipe(device, 560, 300, 560, 1400, 500)
    time.sleep(1)

    if check_all_rallies_on_screen():
        return

    # Scroll down and check 5 times
    for attempt in range(5):
        if not _on_war_screen():
            print(f"[{device}] No longer on war screen — aborting scroll loop")
            return
        adb_swipe(device, 560, 948, 560, 245, 500)
        time.sleep(1)
        if check_all_rallies_on_screen():
            return

    # Timed out - go back (check screen between taps to avoid overshooting)
    print(f"[{device}] No war rallies available")
    for _ in range(4):
        adb_tap(device, 75, 75)
        time.sleep(1)
        if check_screen(device) == "map_screen":
            return
    adb_tap(device, 965, 1865)
    time.sleep(1)
