"""Gold mining and mithril gathering actions.

Leaf module — no dependencies on other action submodules.

Key exports:
    mine_mithril       — full mithril recall+redeploy cycle
    mine_mithril_if_due — run mithril if interval elapsed
    gather_gold        — search + deploy to one gold mine
    gather_gold_loop   — deploy multiple troops to gold mines
"""

import time

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
    (240, 720),   # Mine 1
    (540, 820),   # Mine 2
    (850, 730),   # Mine 3
    (230, 1080),  # Mine 4
    (540, 1200),  # Mine 5
]


@timed_action("mine_mithril")
def mine_mithril(device, stop_check=None):
    """Navigate to Advanced Mithril, recall all troops, redeploy to mines.

    Args:
        stop_check: Optional callable returning True when the task should abort.
    """
    log = get_logger("actions", device)

    def _stopped():
        """Check both explicit stop signal and global mithril toggle."""
        return (stop_check and stop_check()) or not config.MITHRIL_ENABLED

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

    # Step 5: Recall occupied slots — troops are always left-aligned, so
    # returning slot 1 causes the rest to shift left.  Just tap slot 1
    # up to 5 times; stop early when it's empty.
    recalled_count = 0
    slot_x = _MITHRIL_SLOTS_X[0]
    for i in range(5):
        if _stopped():
            break
        adb_tap(device, slot_x, _MITHRIL_SLOT_Y)
        timed_wait(device, lambda: False, 1, "mithril_slot_tap")
        if wait_for_image_and_tap("mithril_return.png", device, timeout=2, threshold=0.7):
            log.debug("Recall %d: RETURN found, recalled", i + 1)
            recalled_count += 1
            timed_wait(device, lambda: False, 1.5, "mithril_recall_anim")
        else:
            log.debug("Recall %d: slot empty, all troops recalled", i + 1)
            break

    if recalled_count > 0:
        log.info("Recalled %d troops from mithril mines", recalled_count)
        timed_wait(device, lambda: False, 1, "mithril_recall_settle")

    if _stopped():
        log.info("Mithril mining aborted after recall (stopped)")
        tap_image("back_arrow.png", device, threshold=0.7)
        tap_image("back_arrow.png", device, threshold=0.7)
        navigate(Screen.MAP, device)
        return False

    # Step 6: Deploy to mines — use device total troops from UI spinbox,
    # fall back to recalled_count if troops were in mines.
    total = config.DEVICE_TOTAL_TROOPS.get(device, 5)
    max_deploys = min(recalled_count if recalled_count > 0 else total, total)
    mines_to_deploy = _MITHRIL_MINES[:max_deploys]
    deployed_count = 0
    for i, (mine_x, mine_y) in enumerate(mines_to_deploy):
        if _stopped():
            break
        log.debug("Deploying to mine %d at (%d, %d)", i + 1, mine_x, mine_y)
        adb_tap(device, mine_x, mine_y)  # Tap mine
        timed_wait(device, lambda: False, 3, "mithril_mine_popup")

        # Look for ATTACK button in the mine popup
        if not wait_for_image_and_tap("mithril_attack.png", device, timeout=5, threshold=0.7):
            save_failure_screenshot(device, f"mithril_no_attack_mine{i+1}")
            if recalled_count == 0:
                # First run — no recall data; no ATTACK likely means no troops left
                log.info("Mine %d: no ATTACK button, no more troops available", i + 1)
                adb_tap(device, 900, 500)  # dismiss popup
                timed_wait(device, lambda: False, 1, "mithril_dismiss_no_attack")
                break
            log.warning("Mine %d: no ATTACK button (occupied or missed)", i + 1)
            adb_tap(device, 900, 500)  # dismiss popup
            timed_wait(device, lambda: False, 1, "mithril_dismiss_occupied")
            continue
        timed_wait(device, lambda: False, 2, "mithril_attack_to_depart")

        # Wait for troop selection screen and tap DEPART
        if wait_for_image_and_tap("mithril_depart.png", device, timeout=4, threshold=0.7):
            deployed_count += 1
            if deployed_count == 1:
                config.MITHRIL_DEPLOY_TIME[device] = time.time()
            timed_wait(device, lambda: False, 2, "mithril_deploy_anim")
        else:
            log.warning("Mine %d: depart button not found after ATTACK", i + 1)
            save_failure_screenshot(device, f"mithril_depart_fail_mine{i+1}")
            adb_tap(device, 900, 500)  # dismiss overlay
            timed_wait(device, lambda: False, 1, "mithril_dismiss_depart_fail")

    log.info("Deployed %d/%d troops to mithril mines", deployed_count, max_deploys)

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
    if not config.MITHRIL_ENABLED:
        return
    last = config.LAST_MITHRIL_TIME.get(device, 0)
    elapsed = time.time() - last
    if elapsed < config.MITHRIL_INTERVAL * 60:
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

    if config.AUTO_HEAL_ENABLED:
        heal_all(device)

    troops = troops_avail(device)
    if troops <= config.MIN_TROOPS_AVAILABLE:
        log.warning("Not enough troops (have %d, need more than %d)",
                    troops, config.MIN_TROOPS_AVAILABLE)
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
    _set_gather_level(device, config.GATHER_MINE_LEVEL)

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
    max_troops = config.GATHER_MAX_TROOPS
    deployed = 0

    for i in range(max_troops):
        if stop_check and stop_check():
            break

        # Ensure we're on MAP for troop count (pixel detection requires MAP)
        if i > 0:
            navigate(Screen.MAP, device)

        troops = troops_avail(device)
        if troops <= config.MIN_TROOPS_AVAILABLE:
            log.info("Not enough troops for more gathers (%d available, min %d)",
                     troops, config.MIN_TROOPS_AVAILABLE)
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
