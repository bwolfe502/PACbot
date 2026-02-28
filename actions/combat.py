"""Basic combat actions: attack, phantom clash, reinforce, target, teleport.

Dependencies: _helpers (for _interruptible_sleep)

Key exports:
    attack              — basic attack sequence
    phantom_clash_attack — Phantom Clash mode attack
    reinforce_throne    — reinforce the throne
    target              — target menu sequence
    teleport            — teleport to random location
    _detect_player_at_eg — player detection near EG positions
"""

import cv2
import numpy as np
import time
import random

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
def phantom_clash_attack(device, stop_check=None):
    """Heal all troops first (if auto heal enabled), then attack in Phantom Clash mode"""
    log = get_logger("actions", device)
    clear_click_trail()
    if config.AUTO_HEAL_ENABLED:
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
