"""Gold mining and mithril gathering actions.

Leaf module — no dependencies on other action submodules.

Key exports:
    mine_mithril       — full mithril recall+redeploy cycle
    mine_mithril_if_due — run mithril if interval elapsed
    gather_gold        — search + deploy to one gold mine
    gather_gold_loop   — deploy multiple troops to gold mines
"""

import time

import cv2
import numpy as np

import config
from config import Screen
from botlog import get_logger, timed_action
from vision import (tap_image, wait_for_image_and_tap, timed_wait,
                    load_screenshot, find_image,
                    adb_tap, adb_swipe, logged_tap,
                    save_failure_screenshot,
                    TAP_OFFSETS, _save_click_trail)
from navigation import navigate, check_screen
from troops import troops_avail, heal_all

_log = get_logger("actions")


# ============================================================
# MITHRIL MINING
# ============================================================

# Fixed coordinates for the Advanced Mithril screen
_MITHRIL_SLOT_Y = 1760
_MITHRIL_SLOTS_X = [138, 339, 540, 741, 942]

_MITHRIL_MINES = [
    (240, 720),   # Mine 1 — top-left
    (540, 820),   # Mine 2 — top-center
    (850, 730),   # Mine 3 — top-right
    (230, 1080),  # Mine 4 — bottom-left
    (540, 1200),  # Mine 5 — bottom-center
    (850, 1080),  # Mine 6 — bottom-right
]

# Region above each mine to check for the red crossed-swords "occupied" icon.
# Icon appears ~130-140px above mine center, within ~80px horizontally.
_OCCUPIED_CHECK_OFFSET_Y = (-180, -60)   # y range relative to mine center
_OCCUPIED_CHECK_OFFSET_X = (-80, 80)     # x range relative to mine center
_OCCUPIED_RED_THRESHOLD = 500            # min red pixels to consider occupied

_MITHRIL_SEARCH_BTN = (410, 1380)       # blue SEARCH button below mines
_MAX_SEARCH_REFRESHES = 3               # max times to refresh for safe mines


def _is_mine_occupied(screen, mine_x, mine_y):
    """Check if a mine has the red crossed-swords icon indicating enemy occupation.

    Scans a region above the mine center for bright red pixels (the swords icon
    is a red circle with crossed swords).  Returns True if enough red pixels are
    found, meaning the mine is occupied by an enemy and should be skipped.
    """
    y1 = max(0, mine_y + _OCCUPIED_CHECK_OFFSET_Y[0])
    y2 = min(screen.shape[0], mine_y + _OCCUPIED_CHECK_OFFSET_Y[1])
    x1 = max(0, mine_x + _OCCUPIED_CHECK_OFFSET_X[0])
    x2 = min(screen.shape[1], mine_x + _OCCUPIED_CHECK_OFFSET_X[1])
    region = screen[y1:y2, x1:x2]
    if region.size == 0:
        return False
    # Red swords icon: high R, low G, low B (OpenCV uses BGR)
    red_mask = (region[:, :, 2] > 180) & (region[:, :, 1] < 100) & (region[:, :, 0] < 100)
    return int(np.sum(red_mask)) >= _OCCUPIED_RED_THRESHOLD


@timed_action("mine_mithril")
def mine_mithril(device, stop_check=None):
    """Navigate to Advanced Mithril, recall all troops, redeploy to mines.

    Args:
        stop_check: Optional callable returning True when the task should abort.
    """
    log = get_logger("actions", device)

    def _stopped():
        """Check both explicit stop signal and global mithril toggle."""
        return (stop_check and stop_check()) or device not in config.MITHRIL_ENABLED_DEVICES

    # Step 1: Navigate to kingdom_screen
    if not navigate(Screen.KINGDOM, device):
        log.warning("Failed to navigate to kingdom screen")
        return False

    if _stopped():
        log.info("Mithril mining aborted (stopped)")
        return False

    # Step 2: Scroll kingdom screen to bottom (multiple swipes for reliability)
    for _ in range(3):
        adb_swipe(device, 540, 960, 540, 400, duration_ms=300)
        timed_wait(device, lambda: False, 0.5, "mithril_scroll_settle")
    timed_wait(device, lambda: False, 1, "mithril_scroll_done")

    if _stopped():
        log.info("Mithril mining aborted (stopped)")
        navigate(Screen.MAP, device)
        return False

    # Step 3: Tap Dimensional Tunnel
    logged_tap(device, 280, 880, "dimensional_tunnel")
    timed_wait(device, lambda: False, 2, "mithril_tunnel_open")

    # Step 4: Tap Advanced Mithril (center of screen)
    logged_tap(device, 540, 960, "advanced_mithril")
    timed_wait(device, lambda: False, 2, "mithril_advanced_open")

    # Clear deploy timer — troops are about to be recalled
    config.MITHRIL_DEPLOY_TIME.pop(device, None)

    if _stopped():
        log.info("Mithril mining aborted (stopped)")
        tap_image("back_arrow.png", device, threshold=0.7)
        tap_image("back_arrow.png", device, threshold=0.7)
        navigate(Screen.MAP, device)
        return False

    # Step 5: Recall troops — tap each slot right-to-left so that plundered
    # or empty slots don't interfere (no dependency on left-shift behavior).
    recalled_count = 0
    for i, slot_x in enumerate(reversed(_MITHRIL_SLOTS_X)):
        if _stopped():
            break
        adb_tap(device, slot_x, _MITHRIL_SLOT_Y)
        timed_wait(device, lambda: False, 1, "mithril_slot_tap")
        if wait_for_image_and_tap("mithril_return.png", device, timeout=2, threshold=0.7):
            log.debug("Recall slot %d: RETURN found, recalled", 5 - i)
            recalled_count += 1
            timed_wait(device, lambda: False, 1.5, "mithril_recall_anim")
        else:
            log.debug("Recall slot %d: empty or plundered, skipping", 5 - i)

    if recalled_count > 0:
        log.info("Recalled %d troops from mithril mines", recalled_count)
        timed_wait(device, lambda: False, 1, "mithril_recall_settle")

    if _stopped():
        log.info("Mithril mining aborted after recall (stopped)")
        tap_image("back_arrow.png", device, threshold=0.7)
        tap_image("back_arrow.png", device, threshold=0.7)
        navigate(Screen.MAP, device)
        return False

    # Step 6: Deploy to mines — scan for safe mines, deploy what we can,
    # then SEARCH to refresh and deploy remaining troops if needed.
    total = config.DEVICE_TOTAL_TROOPS.get(device, 5)
    max_deploys = min(recalled_count if recalled_count > 0 else total, total)
    deployed_count = 0

    for page in range(_MAX_SEARCH_REFRESHES + 1):  # 0 = initial, 1..N = refreshes
        if _stopped() or deployed_count >= max_deploys:
            break

        # Screenshot and find safe (unoccupied) mines on this page
        screen = load_screenshot(device)
        safe_mines = []
        for i, (mine_x, mine_y) in enumerate(_MITHRIL_MINES):
            if _is_mine_occupied(screen, mine_x, mine_y):
                log.debug("Mine %d (%d, %d): enemy occupied — skipping",
                          i + 1, mine_x, mine_y)
            else:
                safe_mines.append((i, mine_x, mine_y))
        remaining = max_deploys - deployed_count
        log.info("Page %d: %d safe mines, %d troops remaining",
                 page + 1, len(safe_mines), remaining)

        # Deploy to safe mines on this page
        for mine_idx, mine_x, mine_y in safe_mines:
            if _stopped() or deployed_count >= max_deploys:
                break
            log.debug("Deploying to mine %d at (%d, %d)",
                      mine_idx + 1, mine_x, mine_y)
            adb_tap(device, mine_x, mine_y)
            timed_wait(device, lambda: False, 3, "mithril_mine_popup")

            # Look for ATTACK button in the mine popup
            if not wait_for_image_and_tap(
                    "mithril_attack.png", device, timeout=5, threshold=0.7):
                save_failure_screenshot(
                    device, f"mithril_no_attack_mine{mine_idx+1}")
                if recalled_count == 0 and deployed_count == 0:
                    log.info("Mine %d: no ATTACK button, no troops available",
                             mine_idx + 1)
                    adb_tap(device, 900, 500)
                    timed_wait(device, lambda: False, 1,
                               "mithril_dismiss_no_attack")
                    break
                log.warning("Mine %d: no ATTACK button (missed tap?)",
                            mine_idx + 1)
                adb_tap(device, 900, 500)
                timed_wait(device, lambda: False, 1,
                           "mithril_dismiss_occupied")
                continue
            timed_wait(device, lambda: False, 2, "mithril_attack_to_depart")

            # Wait for troop selection screen and tap DEPART
            if wait_for_image_and_tap(
                    "mithril_depart.png", device, timeout=4, threshold=0.7):
                deployed_count += 1
                if deployed_count == 1:
                    config.MITHRIL_DEPLOY_TIME[device] = time.time()
                timed_wait(device, lambda: False, 2, "mithril_deploy_anim")
            else:
                log.warning("Mine %d: depart not found after ATTACK",
                            mine_idx + 1)
                save_failure_screenshot(
                    device, f"mithril_depart_fail_mine{mine_idx+1}")
                adb_tap(device, 900, 500)
                timed_wait(device, lambda: False, 1,
                           "mithril_dismiss_depart_fail")

        # If still more troops to deploy, SEARCH for a fresh page
        if deployed_count < max_deploys and page < _MAX_SEARCH_REFRESHES:
            if _stopped():
                break
            log.info("Deployed %d/%d so far — refreshing mines",
                     deployed_count, max_deploys)
            adb_tap(device, *_MITHRIL_SEARCH_BTN)
            timed_wait(device, lambda: False, 3, "mithril_search_refresh")

    log.info("Deployed %d/%d troops to mithril mines",
             deployed_count, max_deploys)

    # Step 7: Navigate back to map screen
    tap_image("back_arrow.png", device, threshold=0.7)  # Back from Advanced Mithril
    tap_image("back_arrow.png", device, threshold=0.7)  # Back from Dimensional Treasure
    navigate(Screen.MAP, device)

    # Step 8: Record timestamp
    config.LAST_MITHRIL_TIME[device] = time.time()

    return deployed_count > 0


def mine_mithril_if_due(device, stop_check=None):
    """Run mithril mining if enabled and interval has elapsed.

    Safe to call frequently — returns immediately if not due.
    Designed to be called from other auto task runners between action cycles.
    """
    if device not in config.MITHRIL_ENABLED_DEVICES:
        return
    last = config.LAST_MITHRIL_TIME.get(device, 0)
    elapsed = time.time() - last
    if elapsed < config.get_device_config(device, "mithril_interval") * 60:
        return
    log = get_logger("actions", device)
    log.info("Mithril mining due (%.0f min since last) — running between actions",
             elapsed / 60)
    mine_mithril(device, stop_check=stop_check)


# ============================================================
# GATHER GOLD
# ============================================================

def _set_gather_level(device, target_level):
    """Tap +/- buttons to set the gold mine level in the search menu.
    Deterministic approach: tap minus to floor the slider, then tap plus to target.
    The slider range is levels 1-6, so 5 minus taps guarantees we hit bottom."""
    log = get_logger("actions", device)

    # Floor the slider by tapping minus enough times to reach level 1
    for _ in range(5):
        logged_tap(device, 320, 1140, "gather_minus_reset")
        time.sleep(0.12)

    # Tap plus (target - 1) times to reach target level from level 1
    for _ in range(target_level - 1):
        logged_tap(device, 965, 1140, "gather_plus_set")
        time.sleep(0.12)

    log.debug("Mine level set to %d (reset + %d plus taps)", target_level, target_level - 1)


@timed_action("gather_gold")
def gather_gold(device, stop_check=None):
    """Search for a gold mine and deploy a troop to gather.

    Flow: MAP -> search button -> gather tab -> set mine level -> search ->
          tap mine on map -> Gather button -> deploy panel -> depart.

    Returns True if a troop was deployed, False otherwise.
    """
    log = get_logger("actions", device)

    if config.get_device_config(device, "auto_heal"):
        heal_all(device)

    troops = troops_avail(device)
    min_troops = config.get_device_config(device, "min_troops")
    if troops <= min_troops:
        log.warning("Not enough troops (have %d, need more than %d)",
                    troops, min_troops)
        return False

    if not navigate(Screen.MAP, device):
        log.warning("Failed to navigate to map screen")
        return False

    if stop_check and stop_check():
        return False

    # Step 1: Open search menu
    logged_tap(device, 900, 1800, "gather_search_btn")
    timed_wait(device, lambda: False, 1.0, "gather_search_menu_open")

    # Step 2: Tap gather/resource tab
    logged_tap(device, 540, 570, "gather_tab")
    timed_wait(device, lambda: False, 0.8, "gather_tab_load")

    # Step 3: Set mine level
    _set_gather_level(device, config.get_device_config(device, "gather_mine_level"))

    if stop_check and stop_check():
        return False

    # Step 4: Tap search
    logged_tap(device, 670, 1390, "gather_search_execute")
    timed_wait(
        device,
        lambda: check_screen(device) == Screen.MAP,
        3, "gather_search_complete")

    # Dismiss any popup
    if check_screen(device) != Screen.MAP:
        log.info("Popup appeared after gather search — navigating back to map")
        if not navigate(Screen.MAP, device):
            log.warning("Failed to dismiss popup and return to map")
            return False

    if stop_check and stop_check():
        return False

    # Step 5-7: Tap mine, tap Gather, wait for depart.
    # Retry the mine+gather sequence once if the first attempt misses.
    for mine_attempt in range(2):
        if stop_check and stop_check():
            return False

        # Step 5: Tap the mine on the map (always centered after search)
        logged_tap(device, 540, 900, "gold_mine_on_map")
        timed_wait(device, lambda: False, 3, "gold_mine_select")

        # Step 6: Tap Gather button (template match — position varies with popup)
        if not wait_for_image_and_tap("gather.png", device, timeout=5, threshold=0.8):
            log.info("Gather button not found — mine tap may have missed")
            if mine_attempt == 0:
                save_failure_screenshot(device, "gather_button_miss")
                navigate(Screen.MAP, device)
                continue
            else:
                log.warning("Gather button not found after 2 attempts")
                save_failure_screenshot(device, "gather_button_fail")
                break

        # Step 7: Wait for deployment panel and tap DEPART
        if wait_for_image_and_tap("depart.png", device, timeout=8, threshold=0.7):
            log.info("Gather Gold troop deployed!")
            return True

        if mine_attempt == 0:
            log.info("Depart not found — retrying mine tap")
            save_failure_screenshot(device, "gather_depart_retry")
            navigate(Screen.MAP, device)
        else:
            log.warning("Failed to find depart button after 2 attempts")
            save_failure_screenshot(device, "gather_depart_fail")

    return False


def gather_gold_loop(device, stop_check=None):
    """Deploy up to GATHER_MAX_TROOPS troops to gold mines.
    Returns the number of troops successfully deployed."""
    log = get_logger("actions", device)
    max_troops = config.get_device_config(device, "gather_max_troops")
    deployed = 0

    for i in range(max_troops):
        if stop_check and stop_check():
            break

        # Ensure we're on MAP for troop count (pixel detection requires MAP)
        if i > 0:
            navigate(Screen.MAP, device)

        troops = troops_avail(device)
        min_troops = config.get_device_config(device, "min_troops")
        if troops <= min_troops:
            log.info("Not enough troops for more gathers (%d available, min %d)",
                     troops, min_troops)
            break

        log.info("Deploying gather troop %d/%d", i + 1, max_troops)
        config.set_device_status(device, f"Gathering Gold ({i+1}/{max_troops})...")

        if gather_gold(device, stop_check=stop_check):
            deployed += 1
        else:
            # Retry once before giving up on this troop slot
            log.info("Gather troop %d failed — retrying once", i + 1)
            navigate(Screen.MAP, device)
            if stop_check and stop_check():
                break
            if gather_gold(device, stop_check=stop_check):
                deployed += 1
            else:
                log.warning("Gather troop %d failed on retry — stopping gather loop", i + 1)
                break

    log.info("Gather loop complete: deployed %d/%d troops", deployed, max_troops)
    return deployed
