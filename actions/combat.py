"""Basic combat actions: attack, phantom clash, reinforce, target, teleport.

Dependencies: _helpers (for _interruptible_sleep)

Key exports:
    attack              — basic attack sequence
    phantom_clash_attack — Phantom Clash mode attack
    reinforce_throne    — reinforce the throne
    target              — target menu sequence
    teleport            — teleport to random location
    teleport_benchmark  — A/B test harness for teleport strategies
    _detect_player_at_eg — player detection near EG positions
"""

import cv2
import json
import numpy as np
import os
import time
import random
from dataclasses import dataclass, field, asdict
from datetime import datetime

import config
from config import Screen
from botlog import get_logger, timed_action
from vision import (tap_image, wait_for_image_and_tap, timed_wait,
                    load_screenshot, find_image, get_template,
                    adb_tap, adb_swipe, logged_tap, clear_click_trail,
                    save_failure_screenshot)
from navigation import navigate, check_screen
from troops import troops_avail, all_troops_home, heal_all

from actions._helpers import _interruptible_sleep

_log = get_logger("actions")


@timed_action("attack")
def attack(device):
    """Heal all troops first (if auto heal enabled), then check troops and attack"""
    log = get_logger("actions", device)
    clear_click_trail()
    if config.get_device_config(device, "auto_heal"):
        heal_all(device)

    if check_screen(device) != Screen.MAP:
        log.debug("Not on map_screen, navigating...")
        if not navigate(Screen.MAP, device):
            log.warning("Failed to navigate to map screen")
            return

    troops = troops_avail(device)
    min_troops = config.get_device_config(device, "min_troops")

    if troops > min_troops:
        logged_tap(device, 560, 675, "attack_selection")
        wait_for_image_and_tap("attack_button.png", device, timeout=5)
        time.sleep(1)  # Wait for attack dialog
        if tap_image("depart.png", device):
            log.info("Attack departed with %d troops available", troops)
        else:
            log.warning("Depart button not found after attack sequence")
    else:
        log.warning("Not enough troops available (have %d, need more than %d)", troops, min_troops)

@timed_action("phantom_clash_attack")
def phantom_clash_attack(device, stop_check=None):
    """Heal all troops first (if auto heal enabled), then attack in Phantom Clash mode"""
    log = get_logger("actions", device)
    clear_click_trail()
    if config.get_device_config(device, "auto_heal"):
        heal_all(device)

    if stop_check and stop_check():
        return

    # Determine if we need to attack based on troop statuses
    screen = load_screenshot(device)
    total = config.DEVICE_TOTAL_TROOPS.get(device, 5)
    deployed = total - troops_avail(device)
    if deployed >= total:
        # All troops out — check if any can attack (stationing or home)
        has_stationing = find_image(screen, "statuses/stationing.png") is not None
        has_battling = find_image(screen, "statuses/battling.png") is not None
        has_marching = find_image(screen, "statuses/marching.png") is not None
        has_returning = find_image(screen, "statuses/returning.png") is not None
        if not has_stationing:
            log.info("All %d troops deployed and none stationing — skipping attack", total)
            return
        log.info("All troops deployed but stationing troop found — proceeding to attack")
    else:
        log.info("%d/%d troops deployed — proceeding to attack", deployed, total)

    # Check for returning troops and drag to recall (skip if attack window already open)
    if not find_image(screen, "esb_middle_attack_window.png"):
        match = find_image(screen, "statuses/returning.png")
        if match:
            _, (mx, my), h, w = match
            cx, cy = mx + w // 2, my + h // 2
            log.info("Found returning troops at (%d, %d), dragging to (560, 1200)", cx, cy)
            adb_swipe(device, cx, cy, 560, 1200, duration_ms=500)
            if _interruptible_sleep(1, stop_check):
                return

    logged_tap(device, 550, 450, "phantom_clash_attack_selection")

    # Wait for the attack menu to open (esb_middle_attack_window.png)
    start = time.time()
    menu_open = False
    while time.time() - start < 31:
        if stop_check and stop_check():
            return
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
        if _interruptible_sleep(1, stop_check):
            return

    if not menu_open:
        log.warning("Timed out waiting for attack menu after 31s")
        return

    # Menu is open but attack button wasn't found yet — keep polling
    if not find_image(load_screenshot(device), "esb_attack.png"):
        while time.time() - start < 31:
            if stop_check and stop_check():
                return
            screen = load_screenshot(device)
            match = find_image(screen, "esb_attack.png")
            if match:
                _, (mx, my), h, w = match
                adb_tap(device, mx + w // 2, my + h // 2)
                log.debug("Tapped esb_attack.png")
                break
            if _interruptible_sleep(1, stop_check):
                return
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
    if config.get_device_config(device, "auto_heal"):
        heal_all(device)

    if check_screen(device) != Screen.MAP:
        log.debug("Not on map_screen, navigating...")
        if not navigate(Screen.MAP, device):
            log.warning("Failed to navigate to map screen")
            return

    troops = troops_avail(device)
    min_troops = config.get_device_config(device, "min_troops")

    if troops > min_troops:
        logged_tap(device, 560, 675, "throne_selection")
        wait_for_image_and_tap("throne_reinforce.png", device, timeout=5)
        time.sleep(1)
        tap_image("depart.png", device)
    else:
        log.warning("Not enough troops available (have %d, need more than %d)", troops, min_troops)

@timed_action("target")
def target(device):
    """Open target menu, tap enemy tab, verify marker exists, then tap target.
    Returns True on success, False on general failure, 'no_marker' if target marker not found.
    """
    log = get_logger("actions", device)
    if config.get_device_config(device, "auto_heal"):
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

def _find_green_pixel(screen, target_color, tolerance=20):
    """Check the center of the screen for the green teleport circle.

    Scans a large region (x:50-1000, y:100-800) to catch the circle regardless
    of camera position.  Samples every 5th pixel for speed and requires at least
    20 matching pixels to avoid false positives from small green UI elements.
    """
    region = screen[100:800:5, 50:1000:5].astype(np.int16)
    diff = np.abs(region - np.array(target_color))
    matches = np.all(diff < tolerance, axis=2)
    return int(np.sum(matches)) >= 20

def _check_green_at_current_position(device, dead_img, stop_check=None):
    """Long-press to open context menu, tap TELEPORT, check for green circle.

    Assumes the camera is already positioned where we want to test.
    Returns (result, screenshot_path, elapsed_s) where result is:
        True  — green circle found
        False — no green circle (normal miss)
        None  — dead.png detected (caller should abort)
    """
    log = get_logger("actions", device)
    target_color = (0, 255, 0)  # BGR green
    start = time.time()

    # Long press to open context menu
    adb_swipe(device, 540, 1400, 540, 1400, 1000)
    time.sleep(2)
    if stop_check and stop_check():
        return False, None, time.time() - start

    # Tap the TELEPORT button on context menu
    logged_tap(device, 780, 1400, "tp_search_btn")
    time.sleep(2)
    if stop_check and stop_check():
        return False, None, time.time() - start

    # Poll for green boundary circle (valid location)
    green_check_start = time.time()
    screen = None
    green_checks = 0

    while time.time() - green_check_start < 3:
        if stop_check and stop_check():
            return False, None, time.time() - start

        screen = load_screenshot(device)
        if screen is None:
            time.sleep(1)
            continue

        if _check_dead(screen, dead_img, device):
            return None, None, time.time() - start

        green_checks += 1
        if _find_green_pixel(screen, target_color):
            elapsed = time.time() - start
            ss_path = save_failure_screenshot(device, "teleport_green_found")
            log.debug("Green circle found after %d checks (%.1fs)", green_checks, elapsed)
            return True, ss_path, elapsed

        time.sleep(1)

    # No green found — cancel
    elapsed = time.time() - start
    log.debug("No green circle after %d checks (%.1fs). Canceling...", green_checks, elapsed)
    if screen is not None:
        match = find_image(screen, "cancel.png")
        if match:
            _, max_loc, h, w = match
            logged_tap(device, max_loc[0] + w // 2, max_loc[1] + h // 2, "tp_cancel")
        else:
            log.debug("Cancel button not found, waiting for UI to clear...")
    time.sleep(2)

    return False, None, elapsed


# Player name colors (BGR) for detecting other players at Evil Guards
_PLAYER_NAME_BLUE = (255, 150, 66)    # #4296FF
_PLAYER_TAG_GOLD  = (115, 215, 255)   # #FFD773

def _detect_player_at_eg(screen, x, y, box_size=200, tolerance=25):
    """Check for another player's name near a dead priest position.

    Player names appear in blue (#4296FF) with gold (#FFD773) alliance tags.
    Both colors must be present in the region to confirm a player is there.
    Returns True if another player is detected.
    """
    h, w = screen.shape[:2]
    x0 = max(x - box_size // 2, 0)
    x1 = min(x + box_size // 2, w)
    y0 = max(y - box_size // 2, 0)
    y1 = min(y + box_size // 2, h)

    region = screen[y0:y1:3, x0:x1:3].astype(np.int16)

    blue_diff = np.abs(region - np.array(_PLAYER_NAME_BLUE))
    blue_matches = np.sum(np.all(blue_diff < tolerance, axis=2))

    gold_diff = np.abs(region - np.array(_PLAYER_TAG_GOLD))
    gold_matches = np.sum(np.all(gold_diff < tolerance, axis=2))

    return blue_matches >= 5 and gold_matches >= 3

@timed_action("teleport")
def teleport(device, dry_run=False):
    """Teleport to a random location on the map.

    Pans camera randomly, long-presses to open context menu, taps TELEPORT,
    then checks for a green boundary circle (valid location). Repeats up to
    15 attempts or 90 seconds.

    If dry_run=True, finds a valid green spot but does NOT tap USE — saves a
    screenshot and cancels instead.  Use for testing without consuming a teleport.
    """
    log = get_logger("actions", device)
    if not dry_run:
        log.debug("Checking if all troops are home before teleporting...")
        if not all_troops_home(device):
            log.warning("Troops are not home! Cannot teleport. Aborting.")
            return False
        if config.get_device_config(device, "auto_heal"):
            heal_all(device)
    else:
        log.info("Teleport DRY RUN — skipping troop check and heal")

    if check_screen(device) != Screen.MAP:
        log.warning("Not on map_screen, can't teleport")
        return False

    log.debug("Starting teleport sequence...")

    # When called from auto-occupy, the camera is centered on the tower we
    # just attacked.  Tapping it opens its info dialog, which auto-pans the
    # camera so the tower moves up and the view centers on the empty area
    # below — a spot more likely to be valid for teleporting.
    # In other contexts this tap may hit nothing (harmless) or an unrelated
    # building (the dialog is dismissed next).  Future: make setup
    # context-aware.
    logged_tap(device, 540, 960, "tp_start")
    time.sleep(2)

    # Load dead image once for reuse
    dead_img = get_template("elements/dead.png")

    # Check for dead before continuing
    screen = load_screenshot(device)
    if _check_dead(screen, dead_img, device):
        return False

    # Dismiss any dialog opened by the tap above
    logged_tap(device, 540, 500, "tp_dismiss_dialog")
    time.sleep(2)

    log.debug("Starting teleport search loop (90 second timeout)...")
    start_time = time.time()
    attempt_count = 0
    max_attempts = 15

    while time.time() - start_time < 90 and attempt_count < max_attempts:
        attempt_count += 1

        # Pan camera randomly (horizontal + vertical)
        distance = random.randint(200, 400)
        dir_x = random.choice([-1, 1])
        dir_y = random.choice([-1, 0, 1])
        end_x = max(100, min(980, 540 + distance * dir_x))
        end_y = max(500, min(1400, 960 + distance * dir_y))
        log.debug("Attempt #%d/%d — pan to (%d, %d)",
                  attempt_count, max_attempts, end_x, end_y)

        adb_swipe(device, 540, 960, end_x, end_y, 300)
        time.sleep(1)

        result, ss_path, elapsed = _check_green_at_current_position(
            device, dead_img)

        if result is None:
            # Dead detected — abort
            return False

        if result:
            total_elapsed = time.time() - start_time
            if dry_run:
                log.info("GREEN CIRCLE FOUND (dry run) on attempt #%d "
                         "(%.1fs). NOT confirming — canceling.",
                         attempt_count, total_elapsed)
                tap_image("cancel.png", device)
                return True

            log.info("Green circle found on attempt #%d (%.1fs). Confirming...",
                     attempt_count, total_elapsed)
            logged_tap(device, 760, 1700, "tp_confirm")
            time.sleep(2)
            log.info("Teleport confirmed after %d attempt(s), %.1fs total",
                     attempt_count, time.time() - start_time)
            return True

        log.debug("Time elapsed: %.1fs / 90s", time.time() - start_time)

    log.error("Teleport failed after %d attempts (%.1fs)",
              attempt_count, time.time() - start_time)
    save_failure_screenshot(device, "teleport_timeout")
    return False


# ============================================================
# TELEPORT BENCHMARK — A/B Test Harness
# ============================================================

@dataclass
class TeleportAttempt:
    """Single attempt within a trial."""
    strategy: str
    attempt_num: int
    success: bool
    elapsed_s: float
    screenshot_path: str = None


@dataclass
class TeleportTrial:
    """One complete trial (up to max_attempts or timeout)."""
    strategy: str
    trial_num: int
    success: bool
    total_attempts: int
    total_time_s: float
    timestamp: str = ""
    attempts: list = field(default_factory=list)


# -- Strategy functions -----------------------------------------------
# Each takes (device, attempt_num) and positions the camera for a check.
# They must NOT call _check_green_at_current_position — that's done by
# _run_trial after the strategy returns.

def _strategy_random_pan(device, attempt_num):
    """Baseline: random 200-400px swipe (current teleport behavior)."""
    distance = random.randint(200, 400)
    dir_x = random.choice([-1, 1])
    dir_y = random.choice([-1, 0, 1])
    end_x = max(100, min(980, 540 + distance * dir_x))
    end_y = max(500, min(1400, 960 + distance * dir_y))
    adb_swipe(device, 540, 960, end_x, end_y, 300)
    time.sleep(1)


def _strategy_big_pan(device, attempt_num):
    """Larger random swipe: 400-700px to cover more ground per attempt."""
    distance = random.randint(400, 700)
    dir_x = random.choice([-1, 1])
    dir_y = random.choice([-1, 0, 1])
    end_x = max(100, min(980, 540 + distance * dir_x))
    end_y = max(300, min(1600, 960 + distance * dir_y))
    adb_swipe(device, 540, 960, end_x, end_y, 300)
    time.sleep(1)


# 8 compass directions for edge_pan
_COMPASS_DIRS = [
    (0, -1),    # N
    (1, -1),    # NE
    (1, 0),     # E
    (1, 1),     # SE
    (0, 1),     # S
    (-1, 1),    # SW
    (-1, 0),    # W
    (-1, -1),   # NW
]

def _strategy_edge_pan(device, attempt_num):
    """Cycle through 8 compass directions with 350-600px swipe."""
    dx, dy = _COMPASS_DIRS[attempt_num % len(_COMPASS_DIRS)]
    distance = random.randint(350, 600)
    end_x = max(100, min(980, 540 + distance * dx))
    end_y = max(300, min(1600, 960 + distance * dy))
    adb_swipe(device, 540, 960, end_x, end_y, 300)
    time.sleep(1)


def _strategy_territory_guided(device, attempt_num):
    """Navigate to TERRITORY, tap a random own/neutral square, return to MAP.

    The tap moves the camera to that tower's location — an area likely to
    be own territory with open space for teleporting.
    """
    log = get_logger("actions", device)
    from config import GRID_WIDTH, GRID_HEIGHT, THRONE_SQUARES

    if not navigate(Screen.TERRITORY, device):
        log.warning("territory_guided: failed to navigate to TERRITORY, "
                    "falling back to random pan")
        if navigate(Screen.MAP, device):
            _strategy_random_pan(device, attempt_num)
        return

    # Pick a random non-throne square
    row = random.randint(0, GRID_HEIGHT - 1)
    col = random.randint(0, GRID_WIDTH - 1)
    while (row, col) in THRONE_SQUARES:
        row = random.randint(0, GRID_HEIGHT - 1)
        col = random.randint(0, GRID_WIDTH - 1)

    # Import here to avoid circular import at module level
    from territory import _get_square_center
    sx, sy = _get_square_center(row, col)
    log.debug("territory_guided: tapping square (%d, %d) at pixel (%d, %d)",
              row, col, sx, sy)
    adb_tap(device, sx, sy)
    time.sleep(2)

    # Return to MAP (the tap should have moved camera to tower area)
    if not navigate(Screen.MAP, device):
        log.warning("territory_guided: failed to return to MAP")
        return
    time.sleep(1)


# Strategy registry
_STRATEGIES = {
    "random_pan": _strategy_random_pan,
    "big_pan": _strategy_big_pan,
    "edge_pan": _strategy_edge_pan,
    "territory_guided": _strategy_territory_guided,
}

_DEFAULT_STRATEGIES = ["random_pan", "big_pan", "edge_pan", "territory_guided"]


def _run_trial(device, strategy_name, strategy_fn, trial_num,
               max_attempts=15, timeout_s=90, stop_check=None):
    """Run a single benchmark trial for the given strategy.

    Always dry_run: cancels on green, never confirms USE.
    Returns a TeleportTrial.
    """
    log = get_logger("actions", device)
    log.info("=== Trial %d: %s ===", trial_num, strategy_name)

    # Navigate to MAP and do setup tap (same as teleport())
    if not navigate(Screen.MAP, device):
        log.warning("Trial %d: failed to navigate to MAP", trial_num)
        return TeleportTrial(
            strategy=strategy_name, trial_num=trial_num, success=False,
            total_attempts=0, total_time_s=0,
            timestamp=datetime.now().isoformat())

    logged_tap(device, 540, 960, "tp_start")
    time.sleep(2)

    dead_img = get_template("elements/dead.png")

    # Check for dead before continuing
    screen = load_screenshot(device)
    if _check_dead(screen, dead_img, device):
        return TeleportTrial(
            strategy=strategy_name, trial_num=trial_num, success=False,
            total_attempts=0, total_time_s=0,
            timestamp=datetime.now().isoformat())

    # Dismiss any dialog
    logged_tap(device, 540, 500, "tp_dismiss_dialog")
    time.sleep(2)

    start_time = time.time()
    attempts = []
    attempt_count = 0

    while time.time() - start_time < timeout_s and attempt_count < max_attempts:
        if stop_check and stop_check():
            log.info("Trial %d: stopped by signal", trial_num)
            break

        attempt_count += 1
        attempt_start = time.time()

        log.debug("Trial %d attempt #%d/%d (%s)",
                  trial_num, attempt_count, max_attempts, strategy_name)

        # Position camera using strategy
        strategy_fn(device, attempt_count - 1)

        # Check for green at current position
        result, ss_path, check_elapsed = _check_green_at_current_position(
            device, dead_img, stop_check=stop_check)

        if result is None:
            # Dead detected — abort trial
            total_time = time.time() - start_time
            log.warning("Trial %d: dead detected, aborting", trial_num)
            return TeleportTrial(
                strategy=strategy_name, trial_num=trial_num, success=False,
                total_attempts=attempt_count,
                total_time_s=round(total_time, 2),
                timestamp=datetime.now().isoformat(),
                attempts=attempts)

        attempt_elapsed = time.time() - attempt_start
        attempt = TeleportAttempt(
            strategy=strategy_name,
            attempt_num=attempt_count,
            success=bool(result),
            elapsed_s=round(attempt_elapsed, 2),
            screenshot_path=ss_path,
        )
        attempts.append(attempt)

        if result:
            total_time = time.time() - start_time
            log.info("Trial %d SUCCESS on attempt #%d (%.1fs total)",
                     trial_num, attempt_count, total_time)
            # Cancel — always dry run in benchmark
            tap_image("cancel.png", device)
            time.sleep(1)
            return TeleportTrial(
                strategy=strategy_name, trial_num=trial_num, success=True,
                total_attempts=attempt_count,
                total_time_s=round(total_time, 2),
                timestamp=datetime.now().isoformat(),
                attempts=attempts)

        log.debug("Trial %d elapsed: %.1fs / %ds",
                  trial_num, time.time() - start_time, timeout_s)

    total_time = time.time() - start_time
    log.info("Trial %d FAILED after %d attempts (%.1fs)",
             trial_num, attempt_count, total_time)

    return TeleportTrial(
        strategy=strategy_name, trial_num=trial_num, success=False,
        total_attempts=attempt_count,
        total_time_s=round(total_time, 2),
        timestamp=datetime.now().isoformat(),
        attempts=attempts)


def _print_benchmark_summary(all_trials):
    """Print a formatted comparison table of benchmark results."""
    log = get_logger("actions")
    strategies = {}
    for trial in all_trials:
        strategies.setdefault(trial.strategy, []).append(trial)

    header = (f"{'Strategy':<22} {'Wins':>6}  {'Win%':>5}  "
              f"{'Avg Time':>9}  {'Avg Atts':>9}  {'Med Atts':>9}")
    separator = "-" * len(header)

    lines = ["\nTeleport Benchmark Results", separator, header, separator]

    for name in _DEFAULT_STRATEGIES:
        if name not in strategies:
            continue
        trials = strategies[name]
        wins = sum(1 for t in trials if t.success)
        total = len(trials)
        win_pct = (wins / total * 100) if total else 0

        successful = [t for t in trials if t.success]
        avg_time = (sum(t.total_time_s for t in successful) /
                    len(successful)) if successful else 0
        avg_atts = (sum(t.total_attempts for t in successful) /
                    len(successful)) if successful else 0

        atts_sorted = sorted(t.total_attempts for t in successful)
        med_atts = (atts_sorted[len(atts_sorted) // 2]
                    if atts_sorted else 0)

        lines.append(
            f"{name:<22} {wins}/{total:>2}    {win_pct:>4.0f}%  "
            f"{avg_time:>8.1f}s  {avg_atts:>9.1f}  {med_atts:>9}")

    lines.append(separator)

    summary = "\n".join(lines)
    log.info(summary)
    print(summary)


def teleport_benchmark(device, trials_per_strategy=3, strategies=None):
    """Run A/B test across teleport camera-positioning strategies.

    Always dry_run — finds valid green spots but never confirms USE.
    Saves structured results to stats/teleport_benchmark_{timestamp}.json.

    Args:
        device: ADB device ID string
        trials_per_strategy: Number of trials per strategy (default 3)
        strategies: List of strategy names to test, or None for all 4
    """
    log = get_logger("actions", device)
    strategy_names = strategies or list(_DEFAULT_STRATEGIES)

    # Discover stop event from running_tasks (works for GUI + webapp)
    stop_check = None
    for key, info in list(config.running_tasks.items()):
        if device in key and "Teleport Benchmark" in key:
            if isinstance(info, dict) and info.get("stop_event"):
                stop_check = info["stop_event"].is_set
                break

    # Validate strategy names
    invalid = [s for s in strategy_names if s not in _STRATEGIES]
    if invalid:
        log.error("Unknown strategies: %s (valid: %s)",
                  invalid, list(_STRATEGIES.keys()))
        return

    log.info("Starting teleport benchmark: %d strategies × %d trials",
             len(strategy_names), trials_per_strategy)

    all_trials = []
    stopped = False

    for strategy_name in strategy_names:
        if stop_check and stop_check():
            stopped = True
            break
        strategy_fn = _STRATEGIES[strategy_name]
        log.info("--- Strategy: %s (%d trials) ---",
                 strategy_name, trials_per_strategy)

        for trial_num in range(1, trials_per_strategy + 1):
            if stop_check and stop_check():
                stopped = True
                break
            trial = _run_trial(device, strategy_name, strategy_fn,
                               trial_num, stop_check=stop_check)
            all_trials.append(trial)
        if stopped:
            break

    # Save results to JSON
    os.makedirs("stats", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_path = f"stats/teleport_benchmark_{timestamp}.json"

    results = {
        "device": device,
        "trials_per_strategy": trials_per_strategy,
        "strategies": strategy_names,
        "timestamp": datetime.now().isoformat(),
        "trials": [asdict(t) for t in all_trials],
    }

    with open(result_path, "w") as f:
        json.dump(results, f, indent=2)
    log.info("Results saved to %s", result_path)

    _print_benchmark_summary(all_trials)
