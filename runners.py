"""Shared task runners for 9Bot.

All auto-mode loop functions live here — used by both the tkinter GUI (main.py)
and the Flask web dashboard (web/dashboard.py). This eliminates the previous
duplication where both files had their own copies of every runner function.

Key exports:
    sleep_interval        — Interruptible sleep with ± variation
    run_auto_quest        — Main farming loop (quests + troops + mithril)
    run_auto_titan        — Rally Titan loop with EG reset
    run_auto_groot        — Join Groot Rally loop
    run_auto_pass         — Pass battle (rally/reinforce/join war)
    run_auto_occupy       — Territory auto-occupy wrapper
    run_auto_reinforce    — Reinforce Throne loop
    run_auto_mithril      — Standalone mithril mining loop
    run_auto_gold         — Gold gathering loop
    run_repeat            — Generic repeating task wrapper
    run_once              — Generic one-shot task wrapper
    launch_task           — Spawn a daemon thread for a task
    stop_task             — Signal a task to stop + set "Stopping ..." status
    force_stop_all        — Force-kill all task threads immediately
    stop_all_tasks_matching — Stop all tasks with a given suffix
"""

import ctypes
import threading
import time
import random

import config
from config import running_tasks, Screen, RallyType
from botlog import get_logger
from navigation import check_screen, navigate
from vision import (adb_tap, load_screenshot, find_image, tap_image,
                    wait_for_image_and_tap)
from troops import troops_avail, heal_all, read_panel_statuses, get_troop_status, TroopAction
from actions import (attack, reinforce_throne, target, check_quests,
                     rally_titan, search_eg_reset, join_rally,
                     join_war_rallies, reset_quest_tracking, reset_rally_blacklist,
                     mine_mithril_if_due, gather_gold_loop)
from territory import auto_occupy_loop


# ============================================================
# UTILITIES
# ============================================================

def sleep_interval(base, variation, stop_check):
    """Sleep for base ± variation seconds, checking stop_check each second."""
    actual = base + random.randint(-variation, variation) if variation > 0 else base
    actual = max(1, actual)
    if variation > 0:
        get_logger("runner").debug("Waiting %ss (base %s +/-%s)", actual, base, variation)
    for _ in range(actual):
        if stop_check():
            break
        time.sleep(1)


def _deployed_status(device):
    """Build a status string from deployed troop actions (e.g. 'Gathering/Defending...')."""
    snapshot = get_troop_status(device)
    if not snapshot:
        return "Waiting for Troops..."
    actions = set()
    for t in snapshot.troops:
        if t.action != TroopAction.HOME:
            actions.add(t.action.value)
    if not actions:
        return "Waiting for Troops..."
    # Title Case, joined by /
    return "/".join(sorted(actions)) + "..."


# Track last check_quests time per device for periodic re-checks
_last_quest_check = {}   # {device: timestamp}
_QUEST_CHECK_INTERVAL = 60  # seconds


def _smart_wait_for_troops(device, stop_check, dlog, max_wait=120):
    """Check troop statuses and wait if one is close to finishing (< max_wait seconds).
    Returns True if a troop became available, False if timed out or stopped."""
    snapshot = read_panel_statuses(device)
    if snapshot is None:
        return False
    soonest = snapshot.soonest_free()
    if soonest is None or soonest.time_left is None:
        return False
    wait_secs = soonest.time_left
    if wait_secs > max_wait:
        dlog.debug("Soonest troop free in %ds — too long, skipping wait", wait_secs)
        return False
    dlog.info("Troop %s finishes in %ds — waiting", soonest.action.value, wait_secs)
    for _ in range(wait_secs + 5):  # Small buffer
        if stop_check():
            return False
        time.sleep(1)
    return True


# ============================================================
# AUTO-MODE RUNNERS
# ============================================================

def run_auto_quest(device, stop_event):
    dlog = get_logger("runner", device)
    dlog.info("Auto Quest started")
    reset_quest_tracking(device)
    reset_rally_blacklist(device)
    stop_check = stop_event.is_set
    lock = config.get_device_lock(device)
    try:
        while not stop_check():
            with lock:
                mine_mithril_if_due(device, stop_check=stop_check)
                if stop_check():
                    break
                # Ensure we're on map_screen before checking troops
                # (troop pixel detection only works on map_screen)
                if not navigate(Screen.MAP, device):
                    dlog.warning("Cannot reach map screen — retrying in 10s")
                    config.set_device_status(device, "Navigating...")
                    for _ in range(10):
                        if stop_check():
                            break
                        time.sleep(1)
                    continue
                read_panel_statuses(device)
                troops = troops_avail(device)
                if troops > config.get_device_config(device, "min_troops"):
                    config.set_device_status(device, "Checking Quests...")
                    check_quests(device, stop_check=stop_check)
                    _last_quest_check[device] = time.time()
                else:
                    # Still run check_quests periodically to keep
                    # dashboard quest tracking up to date
                    since_check = time.time() - _last_quest_check.get(device, 0)
                    if since_check >= _QUEST_CHECK_INTERVAL:
                        config.set_device_status(device, "Checking Quests...")
                        check_quests(device, stop_check=stop_check)
                        _last_quest_check[device] = time.time()
                    else:
                        config.set_device_status(device, _deployed_status(device))
                    if _smart_wait_for_troops(device, stop_check, dlog):
                        continue  # Troop freed up — retry immediately
            if stop_check():
                break
            # Show deployed status if troops are low, otherwise "Idle"
            troops = troops_avail(device) if check_screen(device) == Screen.MAP else 0
            if troops <= config.get_device_config(device, "min_troops"):
                config.set_device_status(device, _deployed_status(device))
            else:
                config.set_device_status(device, "Idle")
            for _ in range(10):
                if stop_check():
                    break
                time.sleep(1)
    except Exception as e:
        dlog.error("ERROR in Auto Quest: %s", e, exc_info=True)
    config.clear_device_status(device)
    dlog.info("Auto Quest stopped")


def run_auto_titan(device, stop_event, interval, variation):
    """Loop rally_titan on a configurable interval.
    Every 5 rallies, searches for an Evil Guard to reset titan distances."""
    dlog = get_logger("runner", device)
    dlog.info("Rally Titan started (interval: %ss +/-%ss)", interval, variation)
    stop_check = stop_event.is_set
    lock = config.get_device_lock(device)
    rally_count = 0
    try:
        while not stop_check():
            with lock:
                mine_mithril_if_due(device, stop_check=stop_check)
                if stop_check():
                    break
                if config.get_device_config(device, "auto_heal"):
                    heal_all(device)
                if not navigate(Screen.MAP, device):
                    dlog.warning("Cannot reach map screen — retrying")
                    config.set_device_status(device, "Navigating...")
                    for _ in range(10):
                        if stop_check():
                            break
                        time.sleep(1)
                    continue
                troops = troops_avail(device)
                if troops > config.get_device_config(device, "min_troops"):
                    # Reset titan distance every 5 rallies by searching for EG
                    if rally_count > 0 and rally_count % 5 == 0:
                        search_eg_reset(device)
                        if stop_check():
                            break
                    config.set_device_status(device, "Rallying Titan...")
                    rally_titan(device)
                    rally_count += 1
                else:
                    dlog.warning("Not enough troops for Rally Titan")
                    config.set_device_status(device, "Waiting for Troops...")
                    if _smart_wait_for_troops(device, stop_check, dlog):
                        continue  # Troop freed up — retry immediately
            if stop_check():
                break
            config.set_device_status(device, "Idle")
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        dlog.error("ERROR in Rally Titan: %s", e, exc_info=True)
    config.clear_device_status(device)
    dlog.info("Rally Titan stopped")


def run_auto_groot(device, stop_event, interval, variation):
    """Loop join_rally('groot') on a configurable interval."""
    dlog = get_logger("runner", device)
    dlog.info("Rally Groot started (interval: %ss +/-%ss)", interval, variation)
    stop_check = stop_event.is_set
    lock = config.get_device_lock(device)
    try:
        while not stop_check():
            with lock:
                mine_mithril_if_due(device, stop_check=stop_check)
                if stop_check():
                    break
                if config.get_device_config(device, "auto_heal"):
                    heal_all(device)
                if not navigate(Screen.MAP, device):
                    dlog.warning("Cannot reach map screen — retrying")
                    config.set_device_status(device, "Navigating...")
                    for _ in range(10):
                        if stop_check():
                            break
                        time.sleep(1)
                    continue
                troops = troops_avail(device)
                if troops > config.get_device_config(device, "min_troops"):
                    config.set_device_status(device, "Joining Groot Rally...")
                    join_rally(RallyType.GROOT, device)
                else:
                    dlog.warning("Not enough troops for Rally Groot")
                    config.set_device_status(device, "Waiting for Troops...")
                    if _smart_wait_for_troops(device, stop_check, dlog):
                        continue  # Troop freed up — retry immediately
            if stop_check():
                break
            config.set_device_status(device, "Idle")
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        dlog.error("ERROR in Rally Groot: %s", e, exc_info=True)
    config.clear_device_status(device)
    dlog.info("Rally Groot stopped")


def run_auto_pass(device, stop_event, pass_mode, pass_interval, variation):
    dlog = get_logger("runner", device)
    stop_check = stop_event.is_set

    def _pass_attack(device):
        if config.get_device_config(device, "auto_heal"):
            heal_all(device)
        troops = troops_avail(device)
        if troops <= config.get_device_config(device, "min_troops"):
            dlog.warning("Not enough troops for pass battle")
            return False

        adb_tap(device, 560, 675)
        time.sleep(1)

        start_time = time.time()
        while time.time() - start_time < 10:
            screen = load_screenshot(device)
            if screen is None:
                time.sleep(0.5)
                continue

            if find_image(screen, "reinforce_button.png", threshold=0.5):
                dlog.info("Found reinforce button - reinforcing")
                tap_image("reinforce_button.png", device, threshold=0.5)
                time.sleep(1)
                tap_image("depart.png", device)
                return "reinforce"

            if find_image(screen, "attack_button.png", threshold=0.7):
                if pass_mode == "Rally Starter":
                    dlog.info("Found attack button - starting rally")
                    tap_image("rally_button.png", device, threshold=0.7)
                    time.sleep(1)
                    if not tap_image("depart.png", device):
                        wait_for_image_and_tap("depart.png", device, timeout=5)
                    return "rally_started"
                else:
                    dlog.info("Found attack button - enemy owns it, closing menu")
                    adb_tap(device, 560, 675)
                    time.sleep(0.5)
                    return "attack"

            time.sleep(0.5)

        dlog.warning("Neither reinforce nor attack button found, closing menu")
        adb_tap(device, 560, 675)
        time.sleep(0.5)
        return False

    dlog.info("Auto Pass Battle started (mode: %s)", pass_mode)
    lock = config.get_device_lock(device)
    try:
        while not stop_check():
            with lock:
                mine_mithril_if_due(device, stop_check=stop_check)
                if stop_check():
                    break
                config.set_device_status(device, "Pass Battle...")
                result = target(device)
                if result == "no_marker":
                    dlog.warning("*** TARGET NOT SET! ***")
                    dlog.warning("Please mark the pass or tower with a Personal 'Enemy' marker.")
                    dlog.warning("Auto Pass Battle stopping.")
                    config.alert_queue.put("no_marker")
                    break
                if stop_check():
                    break
                if not result:
                    break

                action = _pass_attack(device)
            if stop_check():
                break

            if action == "rally_started":
                dlog.info("Rally started - looping back")
                time.sleep(2)
            elif action == "attack":
                dlog.info("Enemy owns pass - joining war rallies continuously")
                config.set_device_status(device, "Joining War Rallies...")
                while not stop_check():
                    with lock:
                        troops = troops_avail(device)
                        if troops <= config.get_device_config(device, "min_troops"):
                            dlog.warning("Not enough troops, waiting...")
                            time.sleep(5)
                            continue
                        join_war_rallies(device)
                    if stop_check():
                        break
                    time.sleep(2)
            elif action == "reinforce":
                sleep_interval(pass_interval, variation, stop_check)
            else:
                sleep_interval(10, variation, stop_check)
    except Exception as e:
        dlog.error("ERROR in Auto Pass Battle: %s", e, exc_info=True)
    config.clear_device_status(device)
    dlog.info("Auto Pass Battle stopped")


def run_auto_occupy(device, stop_event):
    config.auto_occupy_running = True
    config.set_device_status(device, "Occupying Towers...")

    # Monitor stop event in background and set config flag when stopped
    def monitor():
        stop_event.wait()
        config.auto_occupy_running = False

    threading.Thread(target=monitor, daemon=True).start()
    auto_occupy_loop(device)
    config.clear_device_status(device)
    get_logger("runner", device).info("Auto Occupy stopped")


def run_auto_reinforce(device, stop_event, interval, variation):
    """Loop reinforce_throne on a configurable interval."""
    dlog = get_logger("runner", device)
    dlog.info("Auto Reinforce Throne started (interval: %ss +/-%ss)", interval, variation)
    stop_check = stop_event.is_set
    lock = config.get_device_lock(device)
    try:
        while not stop_check():
            with lock:
                mine_mithril_if_due(device, stop_check=stop_check)
                if stop_check():
                    break
                config.set_device_status(device, "Reinforcing Throne...")
                reinforce_throne(device)
            if stop_check():
                break
            config.set_device_status(device, "Idle")
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        dlog.error("ERROR in Auto Reinforce Throne: %s", e, exc_info=True)
    config.clear_device_status(device)
    dlog.info("Auto Reinforce Throne stopped")


def run_auto_mithril(device, stop_event):
    """Standalone mithril mining loop — checks every 60s if mining is due.
    Also useful as fallback when no other auto tasks are running."""
    dlog = get_logger("runner", device)
    dlog.info("Auto Mithril started (interval: %d min)", config.get_device_config(device, "mithril_interval"))
    stop_check = stop_event.is_set
    lock = config.get_device_lock(device)
    try:
        while not stop_check():
            with lock:
                config.set_device_status(device, "Mining Mithril...")
                mine_mithril_if_due(device, stop_check=stop_check)
            if stop_check():
                break
            config.set_device_status(device, "Idle")
            sleep_interval(60, 0, stop_check)  # Check every 60s
    except Exception as e:
        dlog.error("ERROR in Auto Mithril: %s", e, exc_info=True)
    config.clear_device_status(device)
    dlog.info("Auto Mithril stopped")


def run_auto_gold(device, stop_event):
    """Standalone gold gathering loop — deploys troops to gold mines every 60s."""
    dlog = get_logger("runner", device)
    dlog.info("Auto Gold started")
    stop_check = stop_event.is_set
    lock = config.get_device_lock(device)
    try:
        while not stop_check():
            with lock:
                mine_mithril_if_due(device, stop_check=stop_check)
                if stop_check():
                    break
                config.set_device_status(device, "Gathering Gold...")
                if navigate(Screen.MAP, device):
                    gather_gold_loop(device, stop_check=stop_check)
            if stop_check():
                break
            config.set_device_status(device, "Idle")
            sleep_interval(60, 0, stop_check)
    except Exception as e:
        dlog.error("ERROR in Auto Gold: %s", e, exc_info=True)
    config.clear_device_status(device)
    dlog.info("Auto Gold stopped")


# ============================================================
# GENERIC TASK WRAPPERS
# ============================================================

def run_repeat(device, task_name, function, interval, variation, stop_event):
    dlog = get_logger("runner", device)
    stop_check = stop_event.is_set
    lock = config.get_device_lock(device)
    dlog.info("Starting repeating task: %s", task_name)
    try:
        while not stop_check():
            dlog.info("Running %s...", task_name)
            config.set_device_status(device, f"{task_name}...")
            with lock:
                function(device)
            dlog.debug("%s completed, waiting %ss...", task_name, interval)
            config.set_device_status(device, "Idle")
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        dlog.error("ERROR in %s: %s", task_name, e, exc_info=True)
    config.clear_device_status(device)
    dlog.info("%s stopped", task_name)


def run_once(device, task_name, function):
    dlog = get_logger("runner", device)
    lock = config.get_device_lock(device)
    dlog.info("Running %s...", task_name)
    config.set_device_status(device, f"{task_name}...")
    try:
        with lock:
            function(device)
        dlog.info("%s completed", task_name)
    except Exception as e:
        dlog.error("ERROR in %s: %s", task_name, e, exc_info=True)
    config.clear_device_status(device)


# ============================================================
# TASK LAUNCHER
# ============================================================

def launch_task(device, task_name, target_func, stop_event, args=()):
    """Launch a task as a daemon thread."""
    thread = threading.Thread(target=target_func, args=args, daemon=True)
    thread.start()

    task_key = f"{device}_{task_name}"
    running_tasks[task_key] = {"thread": thread, "stop_event": stop_event}
    get_logger("runner", device).info("Started %s", task_name)


# Human-readable labels for auto-mode keys (used in "Stopping ..." status)
_MODE_LABELS = {
    "auto_quest":     "Auto Quest",
    "auto_titan":     "Rally Titans",
    "auto_groot":     "Join Groot",
    "auto_pass":      "Pass Battle",
    "auto_occupy":    "Occupy Towers",
    "auto_reinforce": "Reinforce Throne",
    "auto_mithril":   "Mine Mithril",
    "auto_gold":      "Gather Gold",
}


def stop_task(task_key):
    """Signal a task to stop via its threading.Event and set Stopping status."""
    if task_key in running_tasks:
        info = running_tasks[task_key]
        if isinstance(info, dict) and "stop_event" in info:
            info["stop_event"].set()
            get_logger("runner").debug("Stop signal sent for %s", task_key)
        # Show "Stopping ..." in the device status
        parts = task_key.split("_", 1)
        if len(parts) == 2:
            device, mode_key = parts
            label = _MODE_LABELS.get(mode_key, mode_key)
            config.set_device_status(device, f"Stopping {label}...")


def _force_kill_thread(thread):
    """Force-kill a thread by injecting SystemExit at the next bytecode."""
    if not thread.is_alive():
        return
    tid = thread.ident
    if tid is None:
        return
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(tid), ctypes.py_object(SystemExit))
    if res > 1:
        # Revert — something went wrong
        ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(tid), None)


def force_stop_all():
    """Force-kill every running task thread immediately."""
    _log = get_logger("runner")
    config.auto_occupy_running = False
    config.MITHRIL_ENABLED_DEVICES.clear()
    config.MITHRIL_DEPLOY_TIME.clear()
    for key in list(running_tasks.keys()):
        info = running_tasks.get(key)
        if not isinstance(info, dict):
            continue
        # Set stop event first (cooperative)
        stop_ev = info.get("stop_event")
        if stop_ev:
            stop_ev.set()
        # Force-kill the thread
        thread = info.get("thread")
        if thread:
            _force_kill_thread(thread)
    # Give threads a moment to actually die, then clean up
    time.sleep(0.1)
    running_tasks.clear()
    config.DEVICE_STATUS.clear()
    _log.info("=== ALL TASKS FORCE-KILLED ===")


def stop_all_tasks_matching(suffix):
    """Stop all tasks whose task_key ends with the given suffix."""
    for key in list(running_tasks.keys()):
        if key.endswith(suffix):
            stop_task(key)
