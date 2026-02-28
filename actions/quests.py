"""Quest system, quest tracking, and tower quest.

Uses lazy imports for cross-module dependencies to avoid circular imports.

Key exports:
    check_quests           — main quest check + action orchestration
    get_quest_tracking_state — quest tracking info for web dashboard
    reset_quest_tracking   — clear rally tracking state
    occupy_tower           — occupy a tower for quest
    recall_tower_troop     — recall defending troop
"""

import cv2
import time
import os
import re

import config
from config import QuestType, Screen
from botlog import get_logger, timed_action, stats
from vision import (tap_image, wait_for_image_and_tap, timed_wait,
                    load_screenshot, find_image, get_template,
                    logged_tap, save_failure_screenshot)
from navigation import navigate, check_screen, DEBUG_DIR
from troops import (troops_avail, heal_all, read_panel_statuses,
                    get_troop_status, TroopAction)

from actions._helpers import _interruptible_sleep, _last_depart_slot

_log = get_logger("actions")


# ---- Quest rally tracking ----
# Tracks rallies started but not yet reflected in the quest counter,
# so we don't over-rally while waiting for completion (1-5+ minutes each).
# Pending rallies auto-expire after PENDING_TIMEOUT_S to prevent getting stuck.

_quest_rallies_pending = {}   # e.g. {("127.0.0.1:5555", "titan"): 2}
_quest_last_seen = {}         # e.g. {("127.0.0.1:5555", "titan"): 10}
_quest_target = {}            # e.g. {("127.0.0.1:5555", "titan"): 20}
_quest_pending_since = {}     # e.g. {("127.0.0.1:5555", "titan"): 1708123456.0}

# Troop slot tracking — which troop slots are associated with pending rallies.
# Set before depart, consumed by _record_rally_started.
_quest_rally_slots = {}       # {(device, quest_type): [slot_id, ...]}

PENDING_TIMEOUT_S = config.QUEST_PENDING_TIMEOUT

# ---- Tower quest state ----
# Tracks whether we have a troop defending a tower for quest purposes.
_tower_quest_state = {}   # {device: {"deployed_at": float}}


def _track_quest_progress(device, quest_type, current, target=None):
    """Update pending rally count based on OCR counter progress.
    When the counter advances, we know some pending rallies completed."""
    key = (device, quest_type)
    if target is not None:
        _quest_target[key] = target
    last = _quest_last_seen.get(key)
    if last is not None and current > last:
        completed = current - last
        pending = _quest_rallies_pending.get(key, 0)
        _quest_rallies_pending[key] = max(0, pending - completed)
        if completed > 0:
            _log.info("[%s] %d rally(s) completed (%d->%d), %d still pending",
                      quest_type, completed, last, current, _quest_rallies_pending[key])
            # Pop oldest tracked slots for completed rallies
            slots = _quest_rally_slots.get(key, [])
            for _ in range(min(completed, len(slots))):
                slots.pop(0)
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
    """Record that we started/joined a rally for this quest type.
    Consumes _last_depart_slot[device] if set (from portrait capture before depart)."""
    key = (device, quest_type)
    _quest_rallies_pending[key] = _quest_rallies_pending.get(key, 0) + 1
    slot_id = _last_depart_slot.pop(device, None)
    if slot_id is not None:
        _quest_rally_slots.setdefault(key, []).append(slot_id)
    # Only set timestamp on first pending (don't reset on subsequent)
    if key not in _quest_pending_since:
        _quest_pending_since[key] = time.time()
    _log.info("[%s] Rally started — %d pending (slot=%s)", quest_type,
              _quest_rallies_pending[key], slot_id)


def _effective_remaining(device, quest_type, current, target):
    """How many more rallies we actually need, accounting for in-progress ones."""
    key = (device, quest_type)
    base_remaining = target - current
    pending = _quest_rallies_pending.get(key, 0)
    return max(0, base_remaining - pending)


def get_quest_tracking_state(device):
    """Return quest tracking info for a device (for web dashboard).

    Returns a list of dicts, one per tracked quest type::

        [{"quest_type": "titan", "last_seen": 5, "pending": 2,
          "pending_age": 45.2}, ...]
    """
    result = []
    seen_types = set()
    for (dev, qtype), count in list(_quest_rallies_pending.items()):
        if dev != device:
            continue
        seen_types.add(qtype)
        since = _quest_pending_since.get((dev, qtype))
        result.append({
            "quest_type": str(qtype),
            "last_seen": _quest_last_seen.get((dev, qtype)),
            "target": _quest_target.get((dev, qtype)),
            "pending": count,
            "pending_age": round(time.time() - since, 1) if since else None,
        })
    # Include quest types with a last_seen but no pending
    for (dev, qtype), last in list(_quest_last_seen.items()):
        if dev != device or qtype in seen_types:
            continue
        result.append({
            "quest_type": str(qtype),
            "last_seen": last,
            "target": _quest_target.get((dev, qtype)),
            "pending": 0,
            "pending_age": None,
        })
    return result


def reset_quest_tracking(device=None):
    """Clear rally tracking state. If device is given, clear only that device's state.
    If device is None, clear all state (backwards compatible)."""
    if device is None:
        _quest_rallies_pending.clear()
        _quest_last_seen.clear()
        _quest_target.clear()
        _quest_pending_since.clear()
        _quest_rally_slots.clear()
        _last_depart_slot.clear()
        _tower_quest_state.clear()
    else:
        for d in list(_quest_rallies_pending):
            if d[0] == device:
                del _quest_rallies_pending[d]
        for d in list(_quest_last_seen):
            if d[0] == device:
                del _quest_last_seen[d]
        for d in list(_quest_target):
            if d[0] == device:
                del _quest_target[d]
        for d in list(_quest_pending_since):
            if d[0] == device:
                del _quest_pending_since[d]
        for d in list(_quest_rally_slots):
            if d[0] == device:
                del _quest_rally_slots[d]
        _last_depart_slot.pop(device, None)
        _tower_quest_state.pop(device, None)


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

    from vision import ocr_read
    results = ocr_read(gray, detail=0)
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
        elif quest_type == QuestType.GATHER:
            target = 1000000

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
        skip = " (skip)" if quest_type is None else ""
        cap_note = f" (OCR showed {ocr_target}, cap overridden)" if target != ocr_target else ""
        log.debug("  %s: %d/%d — %s%s%s", quest_type or "unknown", current, target, status, skip, cap_note)

    if not quests:
        log.warning("Quest OCR: no quest patterns found in text")
        return None

    return quests


def _check_quests_legacy(device, stop_check):
    """Legacy PNG-based quest detection. Used as fallback when OCR fails."""
    from actions.rallies import join_rally
    from actions.titans import rally_titan
    from actions.evil_guard import rally_eg
    from actions.combat import attack, target
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
                joined = join_rally([QuestType.EVIL_GUARD, QuestType.TITAN], device, stop_check=stop_check)
            elif quest_img == "eg.png":
                log.info("Attempting to join an Evil Guard rally...")
                joined = join_rally(QuestType.EVIL_GUARD, device, stop_check=stop_check)
            else:
                log.info("Attempting to join a Titan rally...")
                joined = join_rally(QuestType.TITAN, device, stop_check=stop_check)

            if not joined:
                if quest_img == "eg.png":
                    if config.EG_RALLY_OWN_ENABLED:
                        log.info("No rally to join, starting own EG rally")
                        if navigate(Screen.MAP, device):
                            rally_eg(device, stop_check=stop_check)
                    else:
                        log.info("No EG rally to join — own rally disabled, skipping")
                else:
                    if config.TITAN_RALLY_OWN_ENABLED:
                        log.info("No rally to join, starting own Titan rally")
                        if navigate(Screen.MAP, device):
                            rally_titan(device)
                    else:
                        log.info("No Titan rally to join — own rally disabled, skipping")
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
        timed_wait(device,
                   lambda: check_screen(device) == Screen.ALLIANCE_QUEST,
                   1, "aq_claim_settle")
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
        if q["completed"] or q["quest_type"] not in (QuestType.TITAN, QuestType.EVIL_GUARD, QuestType.PVP, QuestType.GATHER, QuestType.FORTRESS, QuestType.TOWER):
            continue
        eff = _effective_remaining(device, q["quest_type"], q["current"], q["target"])
        if eff > 0:
            actionable.append(q)
    return actionable


def _all_quests_visually_complete(device, quests):
    """Check if all quests are visually complete based on OCR counters.

    Uses current >= target (ignoring pending rally tracking).
    Tower/fortress quests are considered OK if a troop is already defending.
    Returns True if gold mining is appropriate."""
    for q in quests:
        qt = q["quest_type"]
        if qt not in (QuestType.TITAN, QuestType.EVIL_GUARD, QuestType.PVP,
                       QuestType.GATHER, QuestType.FORTRESS, QuestType.TOWER):
            continue
        if q["completed"]:
            continue
        if qt in (QuestType.TOWER, QuestType.FORTRESS):
            # Tower is OK as long as a troop is defending
            if _is_troop_defending(device):
                continue
            return False
        # For all other quests, check visual counter
        if q["current"] < q["target"]:
            return False
    return True


def _run_rally_loop(device, actionable, stop_check=None):
    """Execute the rally join/start loop for EG and Titan quests.
    Tries to join rallies first, then starts own rallies if none found.
    Returns True if stop_check was triggered, False otherwise."""
    from actions.rallies import join_rally
    from actions.titans import rally_titan
    from actions.evil_guard import rally_eg
    log = get_logger("actions", device)

    # Build a quick lookup: quest_type -> (current, target) with most remaining
    quest_info = {}
    for q in actionable:
        qt = q["quest_type"]
        if qt in (QuestType.EVIL_GUARD, QuestType.TITAN):
            existing = quest_info.get(qt)
            if existing is None or (q["target"] - q["current"]) > (existing[1] - existing[0]):
                quest_info[qt] = (q["current"], q["target"])

    # Navigate to map first so heal_all doesn't waste time backing out
    navigate(Screen.MAP, device)

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
        type_names = "/".join("Titan" if t == QuestType.TITAN else "Evil Guard" for t in types_to_join)
        config.set_device_status(device, f"Joining {type_names} Rally...")
        joined_type = join_rally(types_to_join, device, skip_heal=True, stop_check=stop_check)
        if stop_check and stop_check():
            return True

        if joined_type:
            _record_rally_started(device, joined_type)
            continue

        # No rally to join — start own rally
        started = False
        if titan_needed > 0:
            if config.TITAN_RALLY_OWN_ENABLED:
                log.info("No rally to join, starting own Titan rally")
                config.set_device_status(device, "Rallying Titan...")
                if navigate(Screen.MAP, device):
                    if rally_titan(device):
                        _record_rally_started(device, QuestType.TITAN)
                        started = True
            else:
                log.info("No Titan rally to join — own rally disabled, skipping")
            if stop_check and stop_check():
                return True
        elif eg_needed > 0:
            if config.EG_RALLY_OWN_ENABLED:
                log.info("No rally to join, starting own EG rally")
                config.set_device_status(device, "Rallying Evil Guard...")
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


def _wait_for_rallies(device, stop_check):
    """Poll map panel for rallying troops instead of re-reading quest counters.

    - Detects false positives: no rallying troops → clears pending immediately.
    - Detects completion: rallying count drops → returns for re-check.
    - Falls back to old behavior if panel read fails (returns without clearing).

    Must be called while on map screen.
    """
    config.set_device_status(device, "Waiting for Rallies...")
    log = get_logger("actions", device)
    wait_start = time.time()

    # Initial panel read
    snapshot = read_panel_statuses(device)
    if snapshot is None:
        log.warning("Panel read failed — falling back to counter polling")
        return

    rallying = snapshot.troops_by_action(TroopAction.RALLYING)
    initial_count = len(rallying)

    if initial_count == 0:
        # No rallying troops — confirm with a second read before clearing
        if _interruptible_sleep(config.RALLY_WAIT_POLL_INTERVAL, stop_check):
            return
        snapshot2 = read_panel_statuses(device)
        if snapshot2 is None:
            log.warning("Panel confirmation read failed — falling back")
            return
        if len(snapshot2.troops_by_action(TroopAction.RALLYING)) == 0:
            # Confirmed: no rallying troops but we think rallies are pending → false positive
            cleared = 0
            for key in list(_quest_rallies_pending):
                if key[0] == device and _quest_rallies_pending[key] > 0:
                    cleared += _quest_rallies_pending[key]
                    _quest_rallies_pending[key] = 0
                    _quest_pending_since.pop(key, None)
                    _quest_rally_slots.pop(key, None)
            elapsed = time.time() - wait_start
            log.warning("No rallying troops on panel — cleared %d phantom pending (%.1fs)", cleared, elapsed)
            stats.record_action(device, "rally_false_positive_cleared", True, elapsed)
        return

    # Rallying troops found — poll until count drops or timeout
    log.info("Panel shows %d rallying troop(s) — polling for completion", initial_count)
    while True:
        if stop_check and stop_check():
            return

        elapsed = time.time() - wait_start
        if elapsed > PENDING_TIMEOUT_S:
            log.warning("Rally wait timed out after %.0fs", elapsed)
            return

        if _interruptible_sleep(config.RALLY_WAIT_POLL_INTERVAL, stop_check):
            return

        snapshot = read_panel_statuses(device)
        if snapshot is None:
            log.warning("Panel read failed during poll — falling back")
            return

        current_count = len(snapshot.troops_by_action(TroopAction.RALLYING))
        if current_count < initial_count:
            elapsed = time.time() - wait_start
            log.info("Rally completion detected via panel (%.1fs) — rallying %d→%d",
                     elapsed, initial_count, current_count)
            stats.record_action(device, "wait_for_rallies", True, elapsed)
            return
        elif current_count > initial_count:
            # Another rally joined while waiting — update baseline
            log.debug("Rallying count increased %d→%d — updating baseline", initial_count, current_count)
            initial_count = current_count


@timed_action("check_quests")
def check_quests(device, stop_check=None):
    """Check alliance/side quests using OCR counter reading, with PNG fallback.
    Reads quest counters (e.g. 'Defeat Titans(0/5)') to determine what needs work.
    Priority: Join EG > Join Titan > Start own Titan > Start own EG > PvP.
    stop_check: optional callable that returns True if we should abort immediately.
    """
    from actions.combat import attack, target
    from actions.farming import gather_gold_loop
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
            if q["quest_type"] in (QuestType.TITAN, QuestType.EVIL_GUARD, QuestType.PVP, QuestType.GATHER, QuestType.TOWER, QuestType.FORTRESS):
                _track_quest_progress(device, q["quest_type"], q["current"], q.get("target"))

        actionable = _get_actionable_quests(device, quests)

        if not actionable:
            # Recall tower troop if all tower quests are done
            if config.TOWER_QUEST_ENABLED:
                _run_tower_quest(device, quests, stop_check)

            # Gold fallback: use visual OCR counters, not pending rally tracking
            if config.GATHER_ENABLED and _all_quests_visually_complete(device, quests):
                log.info("All quests visually complete — gathering gold as fallback")
                if navigate(Screen.MAP, device):
                    gather_gold_loop(device, stop_check)
            else:
                pending_types = [qt for (dev, qt), cnt in _quest_rallies_pending.items() if dev == device and cnt > 0]
                if pending_types:
                    pending_str = ", ".join(f"{qt} ({_quest_rallies_pending[(device, qt)]})" for qt in pending_types)
                    if config.RALLY_PANEL_WAIT_ENABLED:
                        log.info("Pending rallies: %s — watching troop panel", pending_str)
                        if navigate(Screen.MAP, device):
                            _wait_for_rallies(device, stop_check)
                    else:
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
        has_gather = QuestType.GATHER in types_active and config.GATHER_ENABLED
        has_tower = (QuestType.TOWER in types_active or QuestType.FORTRESS in types_active) and config.TOWER_QUEST_ENABLED

        # Handle tower quest first (low cost: 1 troop, no AP, quick deploy)
        if has_tower:
            _run_tower_quest(device, quests, stop_check)
            if stop_check and stop_check():
                return True

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
            # After PVP, deploy gather troops if also needed
            if has_gather and not (stop_check and stop_check()):
                if navigate(Screen.MAP, device):
                    gather_gold_loop(device, stop_check)
            return True

        elif has_gather:
            if navigate(Screen.MAP, device):
                gather_gold_loop(device, stop_check)
            return True

        return True
    else:
        # OCR failed — fall back to PNG matching
        log.warning("OCR failed, falling back to PNG quest detection")
        _check_quests_legacy(device, stop_check)
        return True

# ============================================================
# TOWER QUEST — occupy a tower for alliance quest
# ============================================================

def _is_troop_defending(device):
    """Check if any troop is currently defending (for tower quest).
    Uses cached snapshot if fresh (<30s), otherwise reads panel."""
    snapshot = get_troop_status(device)
    if snapshot is not None and snapshot.age_seconds < 30:
        return snapshot.any_doing(TroopAction.DEFENDING)
    # Need fresh read — must be on map screen
    snapshot = read_panel_statuses(device)
    if snapshot is None:
        return False
    return snapshot.any_doing(TroopAction.DEFENDING)


def _navigate_to_tower(device):
    """Navigate to the tower marked with the in-game target marker.
    Uses the same target menu flow as target() but without heal.
    Returns True if map is now centered on the tower, False on failure."""
    log = get_logger("actions", device)

    if check_screen(device) != Screen.MAP:
        if not navigate(Screen.MAP, device):
            log.warning("Failed to navigate to map screen")
            return False

    if not tap_image("target_menu.png", device):
        log.warning("Tower nav: target_menu.png not found")
        return False
    time.sleep(1)

    # Tap the Enemy tab (tower marker is stored here)
    logged_tap(device, 740, 330, "tower_target_enemy_tab")
    time.sleep(1)

    # Check that a target marker exists
    marker_found = False
    start_time = time.time()
    while time.time() - start_time < 3:
        screen = load_screenshot(device)
        if screen is not None and find_image(screen, "target_marker.png", threshold=0.7):
            marker_found = True
            break
        time.sleep(0.5)

    if not marker_found:
        log.warning("Tower nav: no target marker found — is the tower marked?")
        return False

    # Tap the target to center map on the tower
    logged_tap(device, 350, 476, "tower_target_select")
    time.sleep(2)

    log.info("Navigated to tower via target marker")
    return True


@timed_action("occupy_tower")
def occupy_tower(device, stop_check=None):
    """Occupy a tower for the tower/fortress alliance quest.

    Flow: navigate to marked tower -> tap tower -> reinforce -> depart.
    Returns True if troop deployed or already defending, False on failure.
    """
    log = get_logger("actions", device)

    # Check if already defending
    if navigate(Screen.MAP, device):
        if _is_troop_defending(device):
            log.info("Troop already defending tower — no action needed")
            return True

    if stop_check and stop_check():
        return False

    # Need at least 1 troop
    troops = troops_avail(device)
    if troops < 1:
        log.warning("No troops available to occupy tower")
        return False

    if stop_check and stop_check():
        return False

    # Navigate to the tower via target marker
    config.set_device_status(device, "Tower Quest: Navigating...")
    if not _navigate_to_tower(device):
        log.warning("Failed to navigate to tower")
        return False

    if stop_check and stop_check():
        return False

    # Tap the tower
    config.set_device_status(device, "Tower Quest: Deploying...")
    logged_tap(device, 540, 900, "tower_tap")
    time.sleep(1.5)

    # Tap reinforce
    logged_tap(device, 730, 1430, "tower_reinforce")
    time.sleep(1)

    if stop_check and stop_check():
        return False

    # Tap depart
    if tap_image("depart.png", device):
        log.info("Tower troop deployed!")
        _tower_quest_state[device] = {"deployed_at": time.time()}
        return True
    else:
        log.warning("Depart button not found after tower reinforce")
        save_failure_screenshot(device, "tower_depart_missing")
        return False


@timed_action("recall_tower")
def recall_tower_troop(device, stop_check=None):
    """Recall a troop defending a tower.

    7-step sequence: tap defending icon -> tap tower -> Detail ->
    Recall troops -> Confirm -> close X -> dismiss.
    Returns True on success, False on failure.
    """
    log = get_logger("actions", device)

    if not navigate(Screen.MAP, device):
        log.warning("Failed to navigate to map for tower recall")
        return False

    if stop_check and stop_check():
        return False

    config.set_device_status(device, "Recalling Tower Troop...")

    # Step 1: Tap the defending icon on troop panel to center tower
    if not tap_image("statuses/defending.png", device):
        log.warning("Tower recall: no defending icon found on troop panel")
        return False
    time.sleep(1.5)

    # Step 2: Tap the tower (now centered on screen)
    logged_tap(device, 540, 900, "recall_tower_tap")
    time.sleep(1)

    # Step 3: Tap Detail
    logged_tap(device, 360, 1430, "recall_detail")
    time.sleep(1)

    if stop_check and stop_check():
        return False

    # Step 4: Tap Recall troops
    logged_tap(device, 180, 330, "recall_troops_btn")
    time.sleep(1)

    # Step 5: Tap red Confirm
    logged_tap(device, 315, 1080, "recall_confirm")
    time.sleep(0.5)

    # Step 6: Tap close X
    tap_image("close_x.png", device)
    time.sleep(0.5)

    # Step 7: Tap to dismiss remaining menu
    logged_tap(device, 540, 900, "recall_dismiss")

    _tower_quest_state.pop(device, None)
    log.info("Tower troop recalled")
    return True


def _run_tower_quest(device, quests, stop_check=None):
    """Handle tower/fortress quest: deploy if needed, recall when done.

    If troop is already defending, does nothing.
    If quest is complete and troop is defending, recalls.
    """
    log = get_logger("actions", device)

    # Separate active vs completed tower quests
    tower_quests = [q for q in quests
                    if q["quest_type"] in (QuestType.TOWER, QuestType.FORTRESS)]
    if not tower_quests:
        return

    active = [q for q in tower_quests if not q["completed"]]
    all_done = len(active) == 0

    if all_done:
        # All tower quests completed — recall troop if defending
        if _is_troop_defending(device):
            log.info("Tower quests complete — recalling troop")
            recall_tower_troop(device, stop_check)
        return

    # Tower quest is active
    if _is_troop_defending(device):
        log.info("Tower quest active, troop already defending — skipping")
        config.set_device_status(device, "Tower Quest: Defending...")
        return

    # Need to deploy
    config.set_device_status(device, "Tower Quest: Deploying...")
    success = occupy_tower(device, stop_check)
    if not success:
        log.warning("Tower quest: failed to deploy troop")
