import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import queue
import time
import os
import sys
import random
import json
import logging
import platform
import subprocess

import config
from updater import get_current_version
from config import (set_min_troops, set_auto_heal, set_auto_restore_ap,
                     set_ap_restore_options, set_territory_config, set_eg_rally_own,
                     running_tasks, QuestType, RallyType, Screen)
from devices import get_devices, get_emulator_instances, auto_connect_emulators
from navigation import check_screen, navigate
from vision import adb_tap, tap_image, load_screenshot, find_image, wait_for_image_and_tap, read_ap, warmup_ocr
from troops import troops_avail, heal_all, read_panel_statuses
from actions import (attack, phantom_clash_attack, reinforce_throne, target, check_quests, teleport,
                     rally_titan, rally_eg, search_eg_reset, join_rally,
                     join_war_rallies, reset_quest_tracking, reset_rally_blacklist,
                     test_eg_positions, mine_mithril, mine_mithril_if_due)
from territory import (attack_territory, auto_occupy_loop,
                       open_territory_manager, sample_specific_squares)
from botlog import get_logger

# ============================================================
# PERSISTENT SETTINGS
# ============================================================

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

DEFAULTS = {
    "auto_heal": True,
    "auto_restore_ap": False,
    "ap_use_free": True,
    "ap_use_potions": True,
    "ap_allow_large_potions": True,
    "ap_use_gems": False,
    "ap_gem_limit": 0,
    "min_troops": 0,
    "variation": 0,
    "titan_interval": 30,
    "groot_interval": 30,
    "reinforce_interval": 30,
    "pass_interval": 30,
    "pass_mode": "Rally Joiner",
    "my_team": "yellow",
    "enemy_team": "green",
    "mode": "bl",
    "verbose_logging": False,
    "eg_rally_own": True,
    "mithril_interval": 19,
}

def load_settings():
    _log = get_logger("main")
    try:
        with open(SETTINGS_FILE, "r") as f:
            saved = json.load(f)
        merged = {**DEFAULTS, **saved}
        _log.info("Settings loaded (%d keys, %d from file)", len(merged), len(saved))
        return merged
    except FileNotFoundError:
        _log.info("No settings file found, using defaults (%d keys)", len(DEFAULTS))
        return dict(DEFAULTS)
    except json.JSONDecodeError as e:
        _log.warning("Settings file corrupted (%s), using defaults", e)
        return dict(DEFAULTS)

def save_settings(settings):
    _log = get_logger("main")
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
        _log.debug("Settings saved (%d keys)", len(settings))
    except Exception as e:
        _log.error("Failed to save settings: %s", e)

# ============================================================
# FUNCTION LOOKUP
# ============================================================

TASK_FUNCTIONS = {
    "Rally Titan": rally_titan,
    "Rally Evil Guard": rally_eg,
    "Join Titan Rally": lambda dev: join_rally(QuestType.TITAN, dev),
    "Join Evil Guard Rally": lambda dev: join_rally(QuestType.EVIL_GUARD, dev),
    "Join Groot Rally": lambda dev: join_rally(RallyType.GROOT, dev),
    "Heal All": heal_all,
    "Target": target,
    "Attack": attack,
    "Phantom Clash Attack": phantom_clash_attack,
    "Reinforce Throne": reinforce_throne,
    "UP UP UP!": join_war_rallies,
    "Teleport": teleport,
    "Attack Territory": attack_territory,
    "Check Quests": check_quests,
    "Check Troops": troops_avail,
    "Check Screen": check_screen,
    "Sample Specific Squares": sample_specific_squares,
    "Mine Mithril": mine_mithril,
}

# ============================================================
# TASK RUNNERS (moved from worker.py, using threading.Event)
# ============================================================

# Thread-safe queue for alerts from tasks to GUI
alert_queue = queue.Queue()

def sleep_interval(base, variation, stop_check):
    """Sleep for base ± variation seconds, checking stop_check each second."""
    actual = base + random.randint(-variation, variation) if variation > 0 else base
    actual = max(1, actual)
    if variation > 0:
        get_logger("main").debug("Waiting %ss (base %s +/-%s)", actual, base, variation)
    for _ in range(actual):
        if stop_check():
            break
        time.sleep(1)

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


def run_auto_quest(device, stop_event):
    dlog = get_logger("main", device)
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
                    time.sleep(10)
                    continue
                troops = troops_avail(device)
                if troops > config.MIN_TROOPS_AVAILABLE:
                    check_quests(device, stop_check=stop_check)
                else:
                    dlog.warning("Not enough troops for quests")
                    if _smart_wait_for_troops(device, stop_check, dlog):
                        continue  # Troop freed up — retry immediately
            if stop_check():
                break
            for _ in range(10):
                if stop_check():
                    break
                time.sleep(1)
    except Exception as e:
        dlog.error("ERROR in Auto Quest: %s", e, exc_info=True)
    dlog.info("Auto Quest stopped")

def run_auto_titan(device, stop_event, interval, variation):
    """Loop rally_titan on a configurable interval.
    Every 5 rallies, searches for an Evil Guard to reset titan distances."""
    dlog = get_logger("main", device)
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
                if config.AUTO_HEAL_ENABLED:
                    heal_all(device)
                if not navigate(Screen.MAP, device):
                    dlog.warning("Cannot reach map screen — retrying")
                    time.sleep(10)
                    continue
                troops = troops_avail(device)
                if troops > config.MIN_TROOPS_AVAILABLE:
                    # Reset titan distance every 5 rallies by searching for EG
                    if rally_count > 0 and rally_count % 5 == 0:
                        search_eg_reset(device)
                        if stop_check():
                            break
                    rally_titan(device)
                    rally_count += 1
                else:
                    dlog.warning("Not enough troops for Rally Titan")
                    if _smart_wait_for_troops(device, stop_check, dlog):
                        continue  # Troop freed up — retry immediately
            if stop_check():
                break
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        dlog.error("ERROR in Rally Titan: %s", e, exc_info=True)
    dlog.info("Rally Titan stopped")

def run_auto_groot(device, stop_event, interval, variation):
    """Loop join_rally('groot') on a configurable interval."""
    dlog = get_logger("main", device)
    dlog.info("Rally Groot started (interval: %ss +/-%ss)", interval, variation)
    stop_check = stop_event.is_set
    lock = config.get_device_lock(device)
    try:
        while not stop_check():
            with lock:
                mine_mithril_if_due(device, stop_check=stop_check)
                if stop_check():
                    break
                if config.AUTO_HEAL_ENABLED:
                    heal_all(device)
                if not navigate(Screen.MAP, device):
                    dlog.warning("Cannot reach map screen — retrying")
                    time.sleep(10)
                    continue
                troops = troops_avail(device)
                if troops > config.MIN_TROOPS_AVAILABLE:
                    join_rally(RallyType.GROOT, device)
                else:
                    dlog.warning("Not enough troops for Rally Groot")
                    if _smart_wait_for_troops(device, stop_check, dlog):
                        continue  # Troop freed up — retry immediately
            if stop_check():
                break
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        dlog.error("ERROR in Rally Groot: %s", e, exc_info=True)
    dlog.info("Rally Groot stopped")

def run_auto_occupy(device, stop_event):
    config.auto_occupy_running = True

    # Monitor stop event in background and set config flag when stopped
    def monitor():
        stop_event.wait()
        config.auto_occupy_running = False

    threading.Thread(target=monitor, daemon=True).start()
    auto_occupy_loop(device)
    get_logger("main", device).info("Auto Occupy stopped")

def run_auto_pass(device, stop_event, pass_mode, pass_interval, variation):
    dlog = get_logger("main", device)
    stop_check = stop_event.is_set

    def _pass_attack(device):
        if config.AUTO_HEAL_ENABLED:
            heal_all(device)
        troops = troops_avail(device)
        if troops <= config.MIN_TROOPS_AVAILABLE:
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
                result = target(device)
                if result == "no_marker":
                    dlog.warning("*** TARGET NOT SET! ***")
                    dlog.warning("Please mark the pass or tower with a Personal 'Enemy' marker.")
                    dlog.warning("Auto Pass Battle stopping.")
                    alert_queue.put("no_marker")
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
                while not stop_check():
                    with lock:
                        troops = troops_avail(device)
                        if troops <= config.MIN_TROOPS_AVAILABLE:
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
    dlog.info("Auto Pass Battle stopped")

def run_auto_reinforce(device, stop_event, interval, variation):
    """Loop reinforce_throne on a configurable interval."""
    dlog = get_logger("main", device)
    dlog.info("Auto Reinforce Throne started (interval: %ss +/-%ss)", interval, variation)
    stop_check = stop_event.is_set
    lock = config.get_device_lock(device)
    try:
        while not stop_check():
            with lock:
                mine_mithril_if_due(device, stop_check=stop_check)
                if stop_check():
                    break
                reinforce_throne(device)
            if stop_check():
                break
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        dlog.error("ERROR in Auto Reinforce Throne: %s", e, exc_info=True)
    dlog.info("Auto Reinforce Throne stopped")

def run_auto_mithril(device, stop_event):
    """Standalone mithril mining loop — checks every 60s if mining is due.
    Also useful as fallback when no other auto tasks are running."""
    dlog = get_logger("main", device)
    dlog.info("Auto Mithril started (interval: %d min)", config.MITHRIL_INTERVAL)
    stop_check = stop_event.is_set
    lock = config.get_device_lock(device)
    try:
        while not stop_check():
            with lock:
                mine_mithril_if_due(device, stop_check=stop_check)
            if stop_check():
                break
            sleep_interval(60, 0, stop_check)  # Check every 60s
    except Exception as e:
        dlog.error("ERROR in Auto Mithril: %s", e, exc_info=True)
    dlog.info("Auto Mithril stopped")

def run_repeat(device, task_name, function, interval, variation, stop_event):
    dlog = get_logger("main", device)
    stop_check = stop_event.is_set
    lock = config.get_device_lock(device)
    dlog.info("Starting repeating task: %s", task_name)
    try:
        while not stop_check():
            dlog.info("Running %s...", task_name)
            with lock:
                function(device)
            dlog.debug("%s completed, waiting %ss...", task_name, interval)
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        dlog.error("ERROR in %s: %s", task_name, e, exc_info=True)
    dlog.info("%s stopped", task_name)

def run_once(device, task_name, function):
    dlog = get_logger("main", device)
    lock = config.get_device_lock(device)
    dlog.info("Running %s...", task_name)
    try:
        with lock:
            function(device)
        dlog.info("%s completed", task_name)
    except Exception as e:
        dlog.error("ERROR in %s: %s", task_name, e, exc_info=True)

# ============================================================
# TASK LAUNCHER (threads instead of subprocesses)
# ============================================================

devices = []

def launch_task(device, task_name, target_func, stop_event, args=()):
    """Launch a task as a daemon thread."""
    thread = threading.Thread(target=target_func, args=args, daemon=True)
    thread.start()

    task_key = f"{device}_{task_name}"
    running_tasks[task_key] = {"thread": thread, "stop_event": stop_event}
    get_logger("main", device).info("Started %s", task_name)

def stop_task(task_key):
    """Signal a task to stop via its threading.Event."""
    if task_key in running_tasks:
        info = running_tasks[task_key]
        if isinstance(info, dict) and "stop_event" in info:
            info["stop_event"].set()
            get_logger("main").debug("Stop signal sent for %s", task_key)

def stop_all_tasks_matching(suffix):
    """Stop all tasks whose task_key ends with the given suffix."""
    for key in list(running_tasks.keys()):
        if key.endswith(suffix):
            stop_task(key)

# ============================================================
# GUI
# ============================================================

COLOR_ON = "#2e7d32"
COLOR_OFF = "#6c757d"
COLOR_BG = "#f0f0f0"
COLOR_SECTION_BG = "#e8e8e8"
WIN_WIDTH = 520

# Cross-platform font: "Segoe UI" on Windows, system default on macOS/Linux
_FONT_FAMILY = "Segoe UI" if platform.system() == "Windows" else "Helvetica Neue"
FONT_TOGGLE = (_FONT_FAMILY, 10, "bold")

def make_toggle_bar(parent, text, font_spec, on_click):
    """Create a compact toggle bar."""
    frame = tk.Frame(parent, bg=COLOR_OFF, cursor="hand2")
    label = tk.Label(frame, text=text, font=font_spec,
                     bg=COLOR_OFF, fg="white", pady=5, padx=8)
    label.pack(fill=tk.X)
    frame.bind("<Button-1>", lambda e: on_click())
    label.bind("<Button-1>", lambda e: on_click())
    return frame, label

def create_gui():
    global devices
    log = get_logger("main")

    version = get_current_version()
    settings = load_settings()

    # Set app ID before creating window so taskbar shows our icon
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("pacbot.app")
    except:
        pass

    window = tk.Tk()
    window.title(f"PACbot v{version}")
    window.geometry(f"{WIN_WIDTH}x580")
    window.resizable(False, True)
    window.configure(bg=COLOR_BG)

    # Set window icon (title bar + taskbar)
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    if os.path.isfile(icon_path):
        window.iconbitmap(icon_path)

    PAD_X = 16

    # ── Title ──
    title_frame = tk.Frame(window, bg=COLOR_BG)
    title_frame.pack(fill=tk.X, pady=(10, 4))
    tk.Label(title_frame, text=f"PACbot v{version}", font=(_FONT_FAMILY, 16, "bold"),
             bg=COLOR_BG).pack()
    tk.Label(title_frame, text="Made by Nine", font=(_FONT_FAMILY, 9), fg="#888",
             bg=COLOR_BG).pack()

    def open_tutorial():
        tutorial_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "TUTORIAL.txt")
        if os.path.isfile(tutorial_path):
            if platform.system() == "Windows":
                os.startfile(tutorial_path)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", tutorial_path])
            else:
                subprocess.Popen(["xdg-open", tutorial_path])
        else:
            messagebox.showinfo("Tutorial", "TUTORIAL.txt not found.")

    how_to_label = tk.Label(title_frame, text="How to Use", font=(_FONT_FAMILY, 9, "underline"),
                            fg="#0066cc", cursor="hand2", bg=COLOR_BG)
    how_to_label.pack(pady=(2, 0))
    how_to_label.bind("<Button-1>", lambda e: open_tutorial())

    # ── Devices ──
    device_frame = tk.LabelFrame(window, text="Devices", font=(_FONT_FAMILY, 9, "bold"),
                                  padx=8, pady=4, bg=COLOR_BG)
    device_frame.pack(fill=tk.X, padx=PAD_X, pady=(4, 4))

    device_list_frame = tk.Frame(device_frame, bg=COLOR_BG)
    device_list_frame.pack(fill=tk.X)

    device_checkboxes = {}
    device_checkbox_widgets = []
    device_troops_vars = {}  # {device_id: StringVar} for per-device total troops

    # Load saved per-device troops from settings
    saved_device_troops = settings.get("device_troops", {})

    def _apply_device_troops():
        """Push all device_troops_vars into config.DEVICE_TOTAL_TROOPS."""
        for dev_id, var in device_troops_vars.items():
            try:
                config.DEVICE_TOTAL_TROOPS[dev_id] = int(var.get())
            except ValueError:
                config.DEVICE_TOTAL_TROOPS[dev_id] = 5

    def refresh_device_list():
        global devices
        devices = get_devices()
        instance_map = get_emulator_instances()

        for widget in device_checkbox_widgets:
            widget.destroy()
        device_checkbox_widgets.clear()

        for device in devices:
            if device not in device_checkboxes:
                device_checkboxes[device] = tk.BooleanVar(value=True)

            # Per-device total troops (default 5, restore from saved settings)
            if device not in device_troops_vars:
                saved_val = saved_device_troops.get(device, 5)
                device_troops_vars[device] = tk.StringVar(value=str(saved_val))
                config.DEVICE_TOTAL_TROOPS[device] = saved_val

            display_name = instance_map.get(device, device)

            row = tk.Frame(device_list_frame, bg=COLOR_BG)
            row.pack(fill=tk.X, padx=4)
            device_checkbox_widgets.append(row)

            cb = tk.Checkbutton(row, text=display_name,
                                variable=device_checkboxes[device], font=(_FONT_FAMILY, 9),
                                bg=COLOR_BG, activebackground=COLOR_BG)
            cb.pack(side=tk.LEFT)

            # Troops spinbox (right-aligned)
            tk.Label(row, text="troops:", font=(_FONT_FAMILY, 8), fg="#888",
                     bg=COLOR_BG).pack(side=tk.RIGHT, padx=(4, 0))
            troops_spin = tk.Spinbox(row, from_=1, to=5,
                                     textvariable=device_troops_vars[device],
                                     width=2, font=(_FONT_FAMILY, 8), justify="center",
                                     command=lambda: (_apply_device_troops(), save_current_settings()))
            troops_spin.pack(side=tk.RIGHT)

        _apply_device_troops()

        if not devices:
            lbl = tk.Label(device_list_frame, text="No devices found. Start your emulator and click Refresh.",
                           font=(_FONT_FAMILY, 8), fg="#999", bg=COLOR_BG)
            lbl.pack(pady=2)
            device_checkbox_widgets.append(lbl)

    auto_connect_emulators()
    refresh_device_list()

    btn_row = tk.Frame(device_frame, bg=COLOR_BG)
    btn_row.pack(pady=(2, 0))
    tk.Button(btn_row, text="Refresh", command=lambda: (auto_connect_emulators(), refresh_device_list()),
              font=(_FONT_FAMILY, 8)).pack(side=tk.LEFT, padx=4)
    # Auto-Connect button hidden (crashes on click) but function still available
    # tk.Button(btn_row, text="Auto-Connect",
    #           command=lambda: (auto_connect_emulators(), refresh_device_list()),
    #           font=(_FONT_FAMILY, 8)).pack(side=tk.LEFT, padx=4)

    def get_active_devices():
        return [d for d in devices if device_checkboxes.get(d, tk.BooleanVar(value=False)).get()]

    # ============================================================
    # MODE TOGGLE — Rest Week vs Broken Lands
    # ============================================================

    COLOR_MODE_ACTIVE = "#1a5276"
    COLOR_MODE_INACTIVE = "#bbb"

    mode_var = tk.StringVar(value="bl")

    mode_frame = tk.Frame(window, bg=COLOR_BG)
    mode_frame.pack(fill=tk.X, padx=PAD_X, pady=(6, 4))

    # Use Labels instead of Buttons for mode toggles — tk.Button ignores
    # bg/fg on macOS native theme, making text invisible on dark backgrounds.
    rw_mode_btn = tk.Label(mode_frame, text="Home Server", font=(_FONT_FAMILY, 10, "bold"),
                            bg=COLOR_MODE_INACTIVE, fg="#555", cursor="hand2",
                            padx=12, pady=6, anchor="center")
    rw_mode_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 2))

    bl_mode_btn = tk.Label(mode_frame, text="Broken Lands", font=(_FONT_FAMILY, 10, "bold"),
                            bg=COLOR_MODE_ACTIVE, fg="white", cursor="hand2",
                            padx=12, pady=6, anchor="center")
    bl_mode_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(2, 0))

    # ============================================================
    # AUTO MODES — Two layouts swapped by mode toggle
    # ============================================================
    #
    # BL:   ▼ Combat (Pass, Occupy, Reinforce)  ▼ Farming (Quest, Titans)
    # Home: ▼ Events (Groot)  ▼ Farming (Titans)  ▼ Combat (Reinforce)
    #
    # Toggle bars are created with auto_frame as parent so they can
    # be re-packed into different section containers via in_=.
    # Auto Mithril lives below the settings area, separate from modes.

    auto_frame = tk.Frame(window, bg=COLOR_BG)
    auto_frame.pack(fill=tk.X, padx=PAD_X, pady=(4, 0))

    # -- State variables for all toggles --
    auto_quest_var = tk.BooleanVar(value=False)
    auto_titan_var = tk.BooleanVar(value=False)
    auto_groot_var = tk.BooleanVar(value=False)
    auto_pass_var = tk.BooleanVar(value=False)
    auto_occupy_var = tk.BooleanVar(value=False)
    auto_reinforce_var = tk.BooleanVar(value=False)
    auto_mithril_var = tk.BooleanVar(value=False)

    titan_interval_var = tk.StringVar(value=str(settings["titan_interval"]))
    groot_interval_var = tk.StringVar(value=str(settings["groot_interval"]))
    reinforce_interval_var = tk.StringVar(value=str(settings["reinforce_interval"]))
    pass_mode_var = tk.StringVar(value=settings["pass_mode"])
    pass_interval_var = tk.StringVar(value=str(settings["pass_interval"]))
    mithril_interval_var = tk.StringVar(value=str(settings.get("mithril_interval", 19)))
    config.MITHRIL_INTERVAL = settings.get("mithril_interval", 19)

    # -- helpers to turn each off --
    def _stop_quest():
        if auto_quest_var.get():
            auto_quest_var.set(False)
            quest_frame.config(bg=COLOR_OFF)
            quest_label.config(text="Auto Quest: OFF", bg=COLOR_OFF)
            stop_all_tasks_matching("_auto_quest")
            log.info("Stopping Auto Quest on all devices")

    def _stop_titan():
        if auto_titan_var.get():
            auto_titan_var.set(False)
            titan_frame.config(bg=COLOR_OFF)
            titan_label.config(text="Rally Titans: OFF", bg=COLOR_OFF)
            stop_all_tasks_matching("_auto_titan")
            log.info("Stopping Rally Titans on all devices")

    def _stop_groot():
        if auto_groot_var.get():
            auto_groot_var.set(False)
            groot_frame.config(bg=COLOR_OFF)
            groot_label.config(text="Join Groot: OFF", bg=COLOR_OFF)
            stop_all_tasks_matching("_auto_groot")
            log.info("Stopping Join Groot on all devices")

    def _stop_pass_battle():
        if auto_pass_var.get():
            auto_pass_var.set(False)
            pass_frame.config(bg=COLOR_OFF)
            pass_label.config(text="Pass Battle: OFF", bg=COLOR_OFF)
            stop_all_tasks_matching("_auto_pass")
            log.info("Stopping Pass Battle on all devices")

    def _stop_occupy():
        if auto_occupy_var.get():
            auto_occupy_var.set(False)
            occupy_frame.config(bg=COLOR_OFF)
            occupy_label.config(text="Occupy Towers: OFF", bg=COLOR_OFF)
            config.auto_occupy_running = False
            stop_all_tasks_matching("_auto_occupy")
            log.info("Stopping Occupy Towers on all devices")

    def _stop_reinforce_throne():
        if auto_reinforce_var.get():
            auto_reinforce_var.set(False)
            reinforce_frame.config(bg=COLOR_OFF)
            reinforce_label.config(text="Reinforce Throne: OFF", bg=COLOR_OFF)
            stop_all_tasks_matching("_auto_reinforce")
            log.info("Stopping Reinforce Throne on all devices")

    def _stop_mithril():
        config.MITHRIL_ENABLED = False
        config.MITHRIL_DEPLOY_TIME.clear()
        if auto_mithril_var.get():
            auto_mithril_var.set(False)
            mithril_frame.config(bg=COLOR_OFF)
            mithril_label.config(text="Mine Mithril: OFF", bg=COLOR_OFF)
            stop_all_tasks_matching("_auto_mithril")
            log.info("Stopping Mine Mithril on all devices")

    # ── Collapsible section helper ──
    def _make_section(parent, title, expanded=True):
        """Create a collapsible section with clickable header. Returns (container, inner)."""
        container = tk.Frame(parent, bg=COLOR_BG)
        inner = tk.Frame(container, bg=COLOR_BG, pady=3)
        vis = tk.BooleanVar(value=expanded)
        arrow = "\u25BC" if expanded else "\u25B6"
        btn = tk.Button(container, text=f"  {arrow}  {title.upper()}",
                         font=(_FONT_FAMILY, 8, "bold"), relief=tk.FLAT,
                         bg=COLOR_SECTION_BG, activebackground="#ddd", anchor=tk.W,
                         fg="#555")
        def toggle_section():
            if vis.get():
                inner.pack_forget()
                vis.set(False)
                btn.config(text=f"  \u25B6  {title.upper()}")
            else:
                inner.pack(fill=tk.X)
                vis.set(True)
                btn.config(text=f"  \u25BC  {title.upper()}")
            window.update_idletasks()
            window.geometry(f"{WIN_WIDTH}x{window.winfo_reqheight()}")
        btn.config(command=toggle_section)
        btn.pack(fill=tk.X)
        if expanded:
            inner.pack(fill=tk.X)
        return container, inner

    # ── Section containers (BL has 2, Home has 3) ──
    bl_combat_ctr, bl_combat_inner = _make_section(auto_frame, "Combat")
    bl_farming_ctr, bl_farming_inner = _make_section(auto_frame, "Farming")
    rw_events_ctr, rw_events_inner = _make_section(auto_frame, "Events")
    rw_farming_ctr, rw_farming_inner = _make_section(auto_frame, "Farming")
    rw_combat_ctr, rw_combat_inner = _make_section(auto_frame, "Combat")

    # ── Toggle bars (all parented to auto_frame, re-packed into sections) ──

    def toggle_auto_pass():
        active_devices = get_active_devices()
        if not auto_pass_var.get():
            _stop_quest()
            _stop_occupy()
            auto_pass_var.set(True)
            pass_frame.config(bg=COLOR_ON)
            pass_label.config(text="Pass Battle: ON", bg=COLOR_ON)
            for device in active_devices:
                task_key = f"{device}_auto_pass"
                if task_key not in running_tasks:
                    stop_event = threading.Event()
                    mode = pass_mode_var.get()
                    interval = int(pass_interval_var.get())
                    variation = int(variation_var.get())
                    launch_task(device, "auto_pass",
                                run_auto_pass, stop_event, args=(device, stop_event, mode, interval, variation))
                    log.info("Started Auto Pass Battle on %s", device)
        else:
            _stop_pass_battle()

    pass_frame, pass_label = make_toggle_bar(
        auto_frame, "Pass Battle: OFF", FONT_TOGGLE, toggle_auto_pass)

    def toggle_auto_occupy():
        active_devices = get_active_devices()
        if not auto_occupy_var.get():
            _stop_quest()
            _stop_pass_battle()
            auto_occupy_var.set(True)
            occupy_frame.config(bg=COLOR_ON)
            occupy_label.config(text="Occupy Towers: ON", bg=COLOR_ON)
            for device in active_devices:
                task_key = f"{device}_auto_occupy"
                if task_key not in running_tasks:
                    stop_event = threading.Event()
                    launch_task(device, "auto_occupy",
                                run_auto_occupy, stop_event, args=(device, stop_event))
                    log.info("Started Auto Occupy on %s", device)
        else:
            _stop_occupy()

    occupy_frame, occupy_label = make_toggle_bar(
        auto_frame, "Occupy Towers: OFF", FONT_TOGGLE, toggle_auto_occupy)

    def toggle_auto_reinforce():
        active_devices = get_active_devices()
        if not auto_reinforce_var.get():
            auto_reinforce_var.set(True)
            reinforce_frame.config(bg=COLOR_ON)
            reinforce_label.config(text="Reinforce Throne: ON", bg=COLOR_ON)
            for device in active_devices:
                task_key = f"{device}_auto_reinforce"
                if task_key not in running_tasks:
                    stop_event = threading.Event()
                    interval = int(reinforce_interval_var.get())
                    variation = int(variation_var.get())
                    launch_task(device, "auto_reinforce",
                                run_auto_reinforce, stop_event, args=(device, stop_event, interval, variation))
                    log.info("Started Auto Reinforce Throne on %s", device)
        else:
            _stop_reinforce_throne()

    reinforce_frame, reinforce_label = make_toggle_bar(
        auto_frame, "Reinforce Throne: OFF", FONT_TOGGLE, toggle_auto_reinforce)

    def toggle_auto_quest():
        active_devices = get_active_devices()
        if not auto_quest_var.get():
            _stop_pass_battle()
            _stop_occupy()
            auto_quest_var.set(True)
            quest_frame.config(bg=COLOR_ON)
            quest_label.config(text="Auto Quest: ON", bg=COLOR_ON)
            for device in active_devices:
                task_key = f"{device}_auto_quest"
                if task_key not in running_tasks:
                    stop_event = threading.Event()
                    launch_task(device, "auto_quest",
                                run_auto_quest, stop_event, args=(device, stop_event))
                    log.info("Started Auto Quest on %s", device)
        else:
            _stop_quest()

    quest_frame, quest_label = make_toggle_bar(
        auto_frame, "Auto Quest: OFF", FONT_TOGGLE, toggle_auto_quest)

    def toggle_auto_titan():
        active_devices = get_active_devices()
        if not auto_titan_var.get():
            _stop_groot()
            auto_titan_var.set(True)
            titan_frame.config(bg=COLOR_ON)
            titan_label.config(text="Rally Titans: ON", bg=COLOR_ON)
            for device in active_devices:
                task_key = f"{device}_auto_titan"
                if task_key not in running_tasks:
                    stop_event = threading.Event()
                    interval = int(titan_interval_var.get())
                    variation = int(variation_var.get())
                    launch_task(device, "auto_titan",
                                run_auto_titan, stop_event, args=(device, stop_event, interval, variation))
                    log.info("Started Rally Titan on %s", device)
        else:
            _stop_titan()

    titan_frame, titan_label = make_toggle_bar(
        auto_frame, "Rally Titans: OFF", FONT_TOGGLE, toggle_auto_titan)

    def toggle_auto_groot():
        active_devices = get_active_devices()
        if not auto_groot_var.get():
            _stop_titan()
            auto_groot_var.set(True)
            groot_frame.config(bg=COLOR_ON)
            groot_label.config(text="Join Groot: ON", bg=COLOR_ON)
            for device in active_devices:
                task_key = f"{device}_auto_groot"
                if task_key not in running_tasks:
                    stop_event = threading.Event()
                    interval = int(groot_interval_var.get())
                    variation = int(variation_var.get())
                    launch_task(device, "auto_groot",
                                run_auto_groot, stop_event, args=(device, stop_event, interval, variation))
                    log.info("Started Rally Groot on %s", device)
        else:
            _stop_groot()

    groot_frame, groot_label = make_toggle_bar(
        auto_frame, "Join Groot: OFF", FONT_TOGGLE, toggle_auto_groot)

    def toggle_auto_mithril():
        active_devices = get_active_devices()
        if not auto_mithril_var.get():
            auto_mithril_var.set(True)
            mithril_frame.config(bg=COLOR_ON)
            mithril_label.config(text="Mine Mithril: ON", bg=COLOR_ON)
            config.MITHRIL_ENABLED = True
            config.MITHRIL_INTERVAL = int(mithril_interval_var.get())
            for device in active_devices:
                task_key = f"{device}_auto_mithril"
                if task_key not in running_tasks:
                    stop_event = threading.Event()
                    launch_task(device, "auto_mithril",
                                run_auto_mithril, stop_event, args=(device, stop_event))
                    log.info("Started Mine Mithril on %s", device)
        else:
            _stop_mithril()

    mithril_frame, mithril_label = make_toggle_bar(
        auto_frame, "Mine Mithril: OFF", FONT_TOGGLE, toggle_auto_mithril)

    # ── Row frames for side-by-side layout (children of their section inners) ──
    bl_combat_row1 = tk.Frame(bl_combat_inner, bg=COLOR_BG)     # Pass + Occupy
    bl_combat_row1.pack(fill=tk.X, pady=(0, 3))
    bl_farming_row1 = tk.Frame(bl_farming_inner, bg=COLOR_BG)   # Quest + Titans
    bl_farming_row1.pack(fill=tk.X, pady=(0, 3))
    rw_farming_row1 = tk.Frame(rw_farming_inner, bg=COLOR_BG)   # Titans + Mithril
    rw_farming_row1.pack(fill=tk.X)

    # ── Layout helpers — pack toggle bars into the right sections ──

    def _layout_bl():
        """Pack BL mode: Combat (Pass+Occupy, Reinforce) then Farming (Quest+Titans, Mithril)."""
        bl_combat_ctr.pack(fill=tk.X, in_=auto_frame)
        pass_frame.pack(side=tk.LEFT, expand=True, fill=tk.BOTH, padx=(0, 2), in_=bl_combat_row1)
        occupy_frame.pack(side=tk.LEFT, expand=True, fill=tk.BOTH, padx=(2, 0), in_=bl_combat_row1)
        reinforce_frame.pack(fill=tk.X, in_=bl_combat_inner)

        bl_farming_ctr.pack(fill=tk.X, pady=(4, 0), in_=auto_frame)
        quest_frame.pack(side=tk.LEFT, expand=True, fill=tk.BOTH, padx=(0, 2), in_=bl_farming_row1)
        titan_frame.pack(side=tk.LEFT, expand=True, fill=tk.BOTH, padx=(2, 0), in_=bl_farming_row1)
        mithril_frame.pack(fill=tk.X, in_=bl_farming_inner)

    def _layout_rw():
        """Pack Home Server mode: Events (Groot), Farming (Titans+Mithril), Combat (Reinforce)."""
        rw_events_ctr.pack(fill=tk.X, in_=auto_frame)
        groot_frame.pack(fill=tk.X, in_=rw_events_inner)

        rw_farming_ctr.pack(fill=tk.X, pady=(4, 0), in_=auto_frame)
        titan_frame.pack(side=tk.LEFT, expand=True, fill=tk.BOTH, padx=(0, 2), in_=rw_farming_row1)
        mithril_frame.pack(side=tk.LEFT, expand=True, fill=tk.BOTH, padx=(2, 0), in_=rw_farming_row1)

        rw_combat_ctr.pack(fill=tk.X, pady=(4, 0), in_=auto_frame)
        reinforce_frame.pack(fill=tk.X, in_=rw_combat_inner)

    def _forget_all_toggles():
        """Forget all toggle bars and section containers from layout."""
        for w in [pass_frame, occupy_frame, reinforce_frame, quest_frame,
                  titan_frame, groot_frame, mithril_frame,
                  bl_combat_ctr, bl_farming_ctr,
                  rw_events_ctr, rw_farming_ctr, rw_combat_ctr]:
            w.pack_forget()

    # ── Mode switching ──

    def switch_mode(new_mode):
        if mode_var.get() == new_mode:
            return

        # Don't stop running tasks on mode switch — they continue in the
        # background and their toggle bars reflect the real state when the
        # user switches back.  Only stop tasks exclusive to the OTHER mode
        # that genuinely conflict (currently none do).

        mode_var.set(new_mode)
        _forget_all_toggles()

        if new_mode == "rw":
            _layout_rw()
            bl_settings_row.pack_forget()
            rw_mode_btn.config(bg=COLOR_MODE_ACTIVE, fg="white")
            bl_mode_btn.config(bg=COLOR_MODE_INACTIVE, fg="#555")
        else:
            _layout_bl()
            bl_settings_row.pack(fill=tk.X, pady=(4, 0), in_=settings_frame, before=rw_settings_row)
            bl_mode_btn.config(bg=COLOR_MODE_ACTIVE, fg="white")
            rw_mode_btn.config(bg=COLOR_MODE_INACTIVE, fg="#555")

        window.update_idletasks()
        window.geometry(f"{WIN_WIDTH}x{window.winfo_reqheight()}")

    rw_mode_btn.bind("<Button-1>", lambda e: switch_mode("rw"))
    bl_mode_btn.bind("<Button-1>", lambda e: switch_mode("bl"))

    # Pack initial layout (BL default)
    _layout_bl()

    # ============================================================
    # SETTINGS BAR (compact, mode-aware)
    # ============================================================

    settings_frame = tk.Frame(window, bg=COLOR_SECTION_BG, padx=10, pady=6)
    settings_frame.pack(fill=tk.X, padx=PAD_X, pady=(6, 4))

    tk.Label(settings_frame, text="\u2699  Settings", font=(_FONT_FAMILY, 9, "bold"),
             bg=COLOR_SECTION_BG, fg="#444", anchor=tk.W).pack(fill=tk.X, pady=(0, 4))

    # Row 1: Auto Heal + Restore AP + EG Rally Own + Verbose
    row1 = tk.Frame(settings_frame, bg=COLOR_SECTION_BG)
    row1.pack(fill=tk.X)

    auto_heal_var = tk.BooleanVar(value=settings["auto_heal"])
    set_auto_heal(settings["auto_heal"])

    def toggle_auto_heal():
        set_auto_heal(auto_heal_var.get())
        save_current_settings()

    tk.Checkbutton(row1, text="Auto Heal", variable=auto_heal_var,
                   command=toggle_auto_heal, font=(_FONT_FAMILY, 9),
                   bg=COLOR_SECTION_BG, activebackground=COLOR_SECTION_BG).pack(side=tk.LEFT)

    tk.Frame(row1, width=10, bg=COLOR_SECTION_BG).pack(side=tk.LEFT)

    # Verbose logging toggle
    verbose_var = tk.BooleanVar(value=settings.get("verbose_logging", False))
    from botlog import set_console_verbose
    set_console_verbose(verbose_var.get())

    def toggle_verbose():
        set_console_verbose(verbose_var.get())
        save_current_settings()

    tk.Checkbutton(row1, text="Verbose Log", variable=verbose_var,
                   command=toggle_verbose, font=(_FONT_FAMILY, 9),
                   bg=COLOR_SECTION_BG, activebackground=COLOR_SECTION_BG).pack(side=tk.RIGHT)


    auto_restore_ap_var = tk.BooleanVar(value=settings["auto_restore_ap"])
    set_auto_restore_ap(settings["auto_restore_ap"])

    # AP restore source options (shown when Auto Restore AP is checked)
    ap_settings_row = tk.Frame(settings_frame, bg=COLOR_SECTION_BG)

    ap_use_free_var = tk.BooleanVar(value=settings["ap_use_free"])
    ap_use_potions_var = tk.BooleanVar(value=settings["ap_use_potions"])
    ap_allow_large_var = tk.BooleanVar(value=settings["ap_allow_large_potions"])
    ap_use_gems_var = tk.BooleanVar(value=settings["ap_use_gems"])
    ap_gem_limit_var = tk.StringVar(value=str(settings["ap_gem_limit"]))

    # Apply initial config
    set_ap_restore_options(
        settings["ap_use_free"], settings["ap_use_potions"],
        settings["ap_allow_large_potions"], settings["ap_use_gems"],
        settings["ap_gem_limit"])

    def update_ap_options():
        gem_limit = int(ap_gem_limit_var.get()) if ap_gem_limit_var.get().isdigit() else 0
        set_ap_restore_options(
            ap_use_free_var.get(), ap_use_potions_var.get(),
            ap_allow_large_var.get(), ap_use_gems_var.get(), gem_limit)
        save_current_settings()

    tk.Checkbutton(ap_settings_row, text="Free", variable=ap_use_free_var,
                   command=update_ap_options, font=(_FONT_FAMILY, 8),
                   bg=COLOR_SECTION_BG, activebackground=COLOR_SECTION_BG).pack(side=tk.LEFT)
    tk.Checkbutton(ap_settings_row, text="Potions", variable=ap_use_potions_var,
                   command=update_ap_options, font=(_FONT_FAMILY, 8),
                   bg=COLOR_SECTION_BG, activebackground=COLOR_SECTION_BG).pack(side=tk.LEFT)
    tk.Checkbutton(ap_settings_row, text="Large Potions", variable=ap_allow_large_var,
                   command=update_ap_options, font=(_FONT_FAMILY, 8),
                   bg=COLOR_SECTION_BG, activebackground=COLOR_SECTION_BG).pack(side=tk.LEFT)
    tk.Checkbutton(ap_settings_row, text="Gems", variable=ap_use_gems_var,
                   command=update_ap_options, font=(_FONT_FAMILY, 8),
                   bg=COLOR_SECTION_BG, activebackground=COLOR_SECTION_BG).pack(side=tk.LEFT)
    tk.Label(ap_settings_row, text="Limit:", font=(_FONT_FAMILY, 8),
             bg=COLOR_SECTION_BG).pack(side=tk.LEFT, padx=(4, 0))
    tk.Entry(ap_settings_row, textvariable=ap_gem_limit_var, width=5,
             font=(_FONT_FAMILY, 8)).pack(side=tk.LEFT, padx=(2, 0))
    tk.Button(ap_settings_row, text="Set", command=update_ap_options,
              font=(_FONT_FAMILY, 7)).pack(side=tk.LEFT, padx=(2, 0))

    def toggle_auto_restore_ap():
        enabled = auto_restore_ap_var.get()
        set_auto_restore_ap(enabled)
        if enabled:
            ap_settings_row.pack(fill=tk.X, pady=(2, 0), after=row1)
        else:
            ap_settings_row.pack_forget()
        window.update_idletasks()
        window.geometry(f"{WIN_WIDTH}x{window.winfo_reqheight()}")
        save_current_settings()

    tk.Checkbutton(row1, text="Auto Restore AP", variable=auto_restore_ap_var,
                   command=toggle_auto_restore_ap, font=(_FONT_FAMILY, 9),
                   bg=COLOR_SECTION_BG, activebackground=COLOR_SECTION_BG).pack(side=tk.LEFT)

    # EG rally own toggle
    eg_rally_own_var = tk.BooleanVar(value=settings.get("eg_rally_own", True))
    set_eg_rally_own(settings.get("eg_rally_own", True))

    def toggle_eg_rally_own():
        set_eg_rally_own(eg_rally_own_var.get())
        save_current_settings()

    tk.Checkbutton(row1, text="Rally Own EG", variable=eg_rally_own_var,
                   command=toggle_eg_rally_own, font=(_FONT_FAMILY, 9),
                   bg=COLOR_SECTION_BG, activebackground=COLOR_SECTION_BG).pack(side=tk.LEFT)

    # Show AP settings row if auto restore is already enabled
    if settings["auto_restore_ap"]:
        ap_settings_row.pack(fill=tk.X, pady=(2, 0), after=row1)

    # Row 1b: Min Troops + Randomize
    row1b = tk.Frame(settings_frame, bg=COLOR_SECTION_BG)
    row1b.pack(fill=tk.X, pady=(2, 0))

    tk.Label(row1b, text="Min Troops:", font=(_FONT_FAMILY, 9),
             bg=COLOR_SECTION_BG).pack(side=tk.LEFT)
    min_troops_var = tk.StringVar(value=str(settings["min_troops"]))
    set_min_troops(settings["min_troops"])
    tk.Entry(row1b, textvariable=min_troops_var, width=6,
             font=(_FONT_FAMILY, 9)).pack(side=tk.LEFT, padx=(4, 4))

    def update_min_troops():
        try:
            set_min_troops(int(min_troops_var.get()))
            save_current_settings()
        except:
            pass

    tk.Button(row1b, text="Set", command=update_min_troops,
              font=(_FONT_FAMILY, 8)).pack(side=tk.LEFT)

    variation_var = tk.StringVar(value=str(settings["variation"]))
    tk.Frame(row1b, width=20, bg=COLOR_SECTION_BG).pack(side=tk.LEFT)
    tk.Label(row1b, text="Randomize \u00b1", font=(_FONT_FAMILY, 9),
             bg=COLOR_SECTION_BG).pack(side=tk.LEFT)
    tk.Entry(row1b, textvariable=variation_var, width=4, justify="center",
             font=(_FONT_FAMILY, 9)).pack(side=tk.LEFT, padx=(4, 1))
    tk.Label(row1b, text="s", font=(_FONT_FAMILY, 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)

    tk.Frame(settings_frame, height=1, bg="#ccc").pack(fill=tk.X, pady=(4, 4))

    # Row 2 (BL): Pass mode & interval
    bl_settings_row = tk.Frame(settings_frame, bg=COLOR_SECTION_BG)

    tk.Label(bl_settings_row, text="Pass", font=(_FONT_FAMILY, 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)
    ttk.Combobox(bl_settings_row, textvariable=pass_mode_var,
                 values=["Rally Joiner", "Rally Starter"],
                 width=12, state="readonly", font=(_FONT_FAMILY, 8)).pack(side=tk.LEFT, padx=(4, 4))
    tk.Entry(bl_settings_row, textvariable=pass_interval_var, width=4, justify="center",
             font=(_FONT_FAMILY, 9)).pack(side=tk.LEFT, padx=(0, 1))
    tk.Label(bl_settings_row, text="s", font=(_FONT_FAMILY, 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)

    # Row 2: Titan / Groot / Reinforce intervals (always visible)
    rw_settings_row = tk.Frame(settings_frame, bg=COLOR_SECTION_BG)

    tk.Label(rw_settings_row, text="Titan", font=(_FONT_FAMILY, 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)
    tk.Entry(rw_settings_row, textvariable=titan_interval_var, width=4, justify="center",
             font=(_FONT_FAMILY, 9)).pack(side=tk.LEFT, padx=(4, 1))
    tk.Label(rw_settings_row, text="s", font=(_FONT_FAMILY, 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)

    tk.Frame(rw_settings_row, width=16, bg=COLOR_SECTION_BG).pack(side=tk.LEFT)

    tk.Label(rw_settings_row, text="Groot", font=(_FONT_FAMILY, 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)
    tk.Entry(rw_settings_row, textvariable=groot_interval_var, width=4, justify="center",
             font=(_FONT_FAMILY, 9)).pack(side=tk.LEFT, padx=(4, 1))
    tk.Label(rw_settings_row, text="s", font=(_FONT_FAMILY, 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)

    tk.Frame(rw_settings_row, width=16, bg=COLOR_SECTION_BG).pack(side=tk.LEFT)

    tk.Label(rw_settings_row, text="Reinf", font=(_FONT_FAMILY, 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)
    tk.Entry(rw_settings_row, textvariable=reinforce_interval_var, width=4, justify="center",
             font=(_FONT_FAMILY, 9)).pack(side=tk.LEFT, padx=(4, 1))
    tk.Label(rw_settings_row, text="s", font=(_FONT_FAMILY, 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)

    tk.Frame(rw_settings_row, width=16, bg=COLOR_SECTION_BG).pack(side=tk.LEFT)

    tk.Label(rw_settings_row, text="Mithril", font=(_FONT_FAMILY, 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)
    tk.Entry(rw_settings_row, textvariable=mithril_interval_var, width=4, justify="center",
             font=(_FONT_FAMILY, 9)).pack(side=tk.LEFT, padx=(4, 1))
    tk.Label(rw_settings_row, text="m", font=(_FONT_FAMILY, 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)

    # Pack settings rows (intervals always visible, BL pass row only in BL mode)
    bl_settings_row.pack(fill=tk.X, pady=(4, 0))
    rw_settings_row.pack(fill=tk.X, pady=(4, 0))

    # Apply saved mode (hides BL-only widgets if Home Server)
    if settings["mode"] == "rw":
        switch_mode("rw")

    # ============================================================
    # TERRITORY SETTINGS (collapsed by default)
    # ============================================================

    territory_container = tk.Frame(window, bg=COLOR_BG)
    territory_container.pack(fill=tk.X, padx=PAD_X, pady=(0, 2))

    territory_visible = tk.BooleanVar(value=False)
    territory_inner = tk.Frame(territory_container, padx=8, pady=6, bg=COLOR_SECTION_BG)

    def toggle_territory():
        if territory_visible.get():
            territory_inner.pack_forget()
            territory_visible.set(False)
            territory_btn.config(text="Territory Settings  \u25B6")
            window.update_idletasks()
            window.geometry(f"{WIN_WIDTH}x{window.winfo_reqheight()}")
        else:
            territory_inner.pack(fill=tk.X)
            territory_visible.set(True)
            territory_btn.config(text="Territory Settings  \u25BC")
            window.update_idletasks()
            window.geometry(f"{WIN_WIDTH}x{window.winfo_reqheight()}")

    territory_btn = tk.Button(territory_container, text="Territory Settings  \u25B6",
                               command=toggle_territory, font=(_FONT_FAMILY, 9, "bold"),
                               relief=tk.FLAT, bg=COLOR_SECTION_BG, activebackground="#ddd")
    territory_btn.pack(fill=tk.X)

    # Territory content
    teams_row = tk.Frame(territory_inner, bg=COLOR_SECTION_BG)
    teams_row.pack(fill=tk.X, pady=2)
    tk.Label(teams_row, text="My Team:", font=(_FONT_FAMILY, 9),
             bg=COLOR_SECTION_BG).pack(side=tk.LEFT)
    my_team_var = tk.StringVar(value=settings["my_team"])
    ttk.Combobox(teams_row, textvariable=my_team_var,
                 values=["yellow", "red", "blue", "green"],
                 width=7, state="readonly", font=(_FONT_FAMILY, 8)).pack(side=tk.LEFT, padx=(4, 12))
    tk.Label(teams_row, text="Attack:", font=(_FONT_FAMILY, 9),
             bg=COLOR_SECTION_BG).pack(side=tk.LEFT)
    enemy_var = tk.StringVar(value=settings["enemy_team"])
    ttk.Combobox(teams_row, textvariable=enemy_var,
                 values=["green", "red", "blue", "yellow"],
                 width=7, state="readonly", font=(_FONT_FAMILY, 8)).pack(side=tk.LEFT, padx=(4, 6))

    set_territory_config(settings["my_team"], [settings["enemy_team"]])

    def update_territory_config():
        set_territory_config(my_team_var.get(), [enemy_var.get()])
        save_current_settings()

    tk.Button(teams_row, text="Set", command=update_territory_config,
              font=(_FONT_FAMILY, 8)).pack(side=tk.LEFT)

    def open_territory_mgr():
        active = get_active_devices()
        if active:
            threading.Thread(target=open_territory_manager, args=(active[0],), daemon=True).start()

    tk.Button(territory_inner, text="Territory Square Manager",
              command=open_territory_mgr, font=(_FONT_FAMILY, 9)).pack(fill=tk.X, pady=(4, 0))

    # ============================================================
    # SETTINGS PERSISTENCE
    # ============================================================

    def save_current_settings():
        # Build per-device troops dict
        dt = {}
        for dev_id, var in device_troops_vars.items():
            try:
                dt[dev_id] = int(var.get())
            except ValueError:
                dt[dev_id] = 5

        save_settings({
            "auto_heal": auto_heal_var.get(),
            "auto_restore_ap": auto_restore_ap_var.get(),
            "ap_use_free": ap_use_free_var.get(),
            "ap_use_potions": ap_use_potions_var.get(),
            "ap_allow_large_potions": ap_allow_large_var.get(),
            "ap_use_gems": ap_use_gems_var.get(),
            "ap_gem_limit": int(ap_gem_limit_var.get()) if ap_gem_limit_var.get().isdigit() else 0,
            "min_troops": int(min_troops_var.get()) if min_troops_var.get().isdigit() else 0,
            "variation": int(variation_var.get()) if variation_var.get().isdigit() else 0,
            "titan_interval": int(titan_interval_var.get()) if titan_interval_var.get().isdigit() else 30,
            "groot_interval": int(groot_interval_var.get()) if groot_interval_var.get().isdigit() else 30,
            "reinforce_interval": int(reinforce_interval_var.get()) if reinforce_interval_var.get().isdigit() else 30,
            "pass_interval": int(pass_interval_var.get()) if pass_interval_var.get().isdigit() else 30,
            "pass_mode": pass_mode_var.get(),
            "my_team": my_team_var.get(),
            "enemy_team": enemy_var.get(),
            "mode": mode_var.get(),
            "verbose_logging": verbose_var.get(),
            "eg_rally_own": eg_rally_own_var.get(),
            "mithril_interval": int(mithril_interval_var.get()) if mithril_interval_var.get().isdigit() else 19,
            "device_troops": dt,
        })

    # ============================================================
    # MORE ACTIONS (collapsed by default)
    # ============================================================

    actions_container = tk.Frame(window, bg=COLOR_BG)
    actions_container.pack(fill=tk.BOTH, expand=True, padx=PAD_X, pady=(2, 0))

    actions_visible = tk.BooleanVar(value=False)
    actions_inner = tk.Frame(actions_container, bg=COLOR_SECTION_BG)

    def toggle_actions():
        if actions_visible.get():
            actions_inner.pack_forget()
            actions_visible.set(False)
            actions_toggle_btn.config(text="More Actions  \u25B6")
            window.update_idletasks()
            window.geometry(f"{WIN_WIDTH}x{window.winfo_reqheight()}")
        else:
            actions_inner.pack(fill=tk.BOTH, expand=True)
            actions_visible.set(True)
            actions_toggle_btn.config(text="More Actions  \u25BC")
            window.update_idletasks()
            window.geometry(f"{WIN_WIDTH}x{window.winfo_reqheight()}")

    actions_toggle_btn = tk.Button(actions_container, text="More Actions  \u25B6",
                                    command=toggle_actions, font=(_FONT_FAMILY, 9, "bold"),
                                    relief=tk.FLAT, bg=COLOR_SECTION_BG, activebackground="#ddd")
    actions_toggle_btn.pack(fill=tk.X)

    # Tabs inside collapsible section
    tab_style = ttk.Style()
    tab_style.configure("Bold.TNotebook.Tab", font=(_FONT_FAMILY, 10, "bold"), padding=[12, 6])

    tabs = ttk.Notebook(actions_inner, style="Bold.TNotebook")
    tabs.pack(fill=tk.BOTH, expand=True, pady=(4, 4), padx=4)

    farm_tab = tk.Frame(tabs, padx=4, pady=4)
    war_tab = tk.Frame(tabs, padx=4, pady=4)
    debug_tab = tk.Frame(tabs, padx=4, pady=4)
    tabs.add(farm_tab, text="  Farm  ")
    tabs.add(war_tab, text="  War  ")
    tabs.add(debug_tab, text="  Debug  ")

    # ── Task row helpers ──
    task_row_enabled_vars = []

    def add_task_row(parent, name, default_interval):
        frame = tk.Frame(parent)
        frame.pack(pady=2, fill=tk.X)

        enabled = tk.BooleanVar()
        task_row_enabled_vars.append(enabled)
        interval = tk.StringVar(value=str(default_interval))

        def toggle():
            if enabled.get():
                func = TASK_FUNCTIONS.get(name)
                if not func:
                    log.warning("Unknown function: %s", name)
                    return
                for device in get_active_devices():
                    task_key = f"{device}_repeat:{name}"
                    if task_key not in running_tasks:
                        stop_event = threading.Event()
                        iv = int(interval.get())
                        vr = int(variation_var.get())
                        launch_task(device, f"repeat:{name}",
                                    run_repeat, stop_event, args=(device, name, func, iv, vr, stop_event))
            else:
                log.info("Stopping %s", name)
                for device in get_active_devices():
                    task_key = f"{device}_repeat:{name}"
                    stop_task(task_key)

        tk.Checkbutton(frame, variable=enabled, command=toggle).pack(side=tk.LEFT)
        tk.Entry(frame, textvariable=interval, width=4, justify="center",
                 font=(_FONT_FAMILY, 8)).pack(side=tk.LEFT, padx=(2, 0))
        tk.Label(frame, text="s", font=(_FONT_FAMILY, 8)).pack(side=tk.LEFT, padx=(1, 4))

        def do_run_once():
            enabled.set(False)
            func = TASK_FUNCTIONS.get(name)
            if not func:
                log.warning("Unknown function: %s", name)
                return
            for device in get_active_devices():
                repeat_key = f"{device}_repeat:{name}"
                stop_task(repeat_key)
                stop_event = threading.Event()
                launch_task(device, f"once:{name}",
                            run_once, stop_event, args=(device, name, func))

        tk.Button(frame, text=name, command=do_run_once,
                  font=(_FONT_FAMILY, 9)).pack(side=tk.LEFT, fill=tk.X, expand=True)

    def add_debug_button(parent, name, function):
        def do_run_once():
            for device in get_active_devices():
                threading.Thread(target=function, args=(device,), daemon=True).start()

        tk.Button(parent, text=name, command=do_run_once,
                  font=(_FONT_FAMILY, 9)).pack(pady=2, fill=tk.X)

    # Farm tab (Rally Titan removed — now a top-level toggle)
    add_task_row(farm_tab, "Rally Evil Guard", 30)
    add_task_row(farm_tab, "Join Titan Rally", 30)
    add_task_row(farm_tab, "Join Evil Guard Rally", 30)
    add_task_row(farm_tab, "Join Groot Rally", 30)
    add_task_row(farm_tab, "Heal All", 30)

    # War tab
    add_task_row(war_tab, "Target", 30)
    add_task_row(war_tab, "Attack", 30)
    add_task_row(war_tab, "Phantom Clash Attack", 30)
    add_task_row(war_tab, "Reinforce Throne", 30)
    add_task_row(war_tab, "UP UP UP!", 30)
    add_task_row(war_tab, "Teleport", 30)
    add_task_row(war_tab, "Attack Territory", 30)

    # Debug tab
    def save_screenshot(device):
        """Save a screenshot to disk for cropping element images."""
        import cv2
        screen = load_screenshot(device)
        if screen is not None:
            path = f"screenshot_{device.replace(':', '_')}.png"
            cv2.imwrite(path, screen)
            get_logger("main", device).info("Screenshot saved to %s", path)
        else:
            get_logger("main", device).warning("Failed to take screenshot")

    add_debug_button(debug_tab, "Save Screenshot", save_screenshot)
    add_debug_button(debug_tab, "Check Quests", check_quests)
    add_debug_button(debug_tab, "Check Troops", troops_avail)

    def debug_troop_status(device):
        """Read and log full troop panel statuses."""
        snapshot = read_panel_statuses(device)
        dlog = get_logger("main", device)
        if snapshot is None:
            dlog.info("Troop Status: could not read (not on map screen?)")
            return
        for i, t in enumerate(snapshot.troops, 1):
            if t.is_home:
                dlog.info("  Troop %d: HOME", i)
            elif t.time_left is not None:
                dlog.info("  Troop %d: %s (%ds left)", i, t.action.value, t.time_left)
            else:
                dlog.info("  Troop %d: %s", i, t.action.value)
        dlog.info("Troop Status: %d home, %d deployed", snapshot.home_count, snapshot.deployed_count)

    add_debug_button(debug_tab, "Troop Status", debug_troop_status)
    add_debug_button(debug_tab, "Check AP", read_ap)
    add_debug_button(debug_tab, "Check Screen", check_screen)
    add_debug_button(debug_tab, "Test EG Positions", test_eg_positions)
    add_debug_button(debug_tab, "Attack Territory (Debug)", lambda dev: attack_territory(dev, debug=True))
    add_debug_button(debug_tab, "Sample Specific Squares", sample_specific_squares)
    add_debug_button(debug_tab, "Mine Mithril", mine_mithril)

    tk.Frame(debug_tab, height=1, bg="gray80").pack(fill=tk.X, pady=6)

    tap_row = tk.Frame(debug_tab)
    tap_row.pack(fill=tk.X, pady=2)
    tk.Label(tap_row, text="Tap X:", font=(_FONT_FAMILY, 9)).pack(side=tk.LEFT)
    x_var = tk.StringVar()
    tk.Entry(tap_row, textvariable=x_var, width=5, font=(_FONT_FAMILY, 8)).pack(side=tk.LEFT, padx=(2, 8))
    tk.Label(tap_row, text="Y:", font=(_FONT_FAMILY, 9)).pack(side=tk.LEFT)
    y_var = tk.StringVar()
    tk.Entry(tap_row, textvariable=y_var, width=5, font=(_FONT_FAMILY, 8)).pack(side=tk.LEFT, padx=(2, 8))

    def test_tap():
        try:
            x, y = int(x_var.get()), int(y_var.get())
            for device in get_active_devices():
                adb_tap(device, x, y)
                get_logger("main", device).debug("Test tapped (%s, %s)", x, y)
        except ValueError:
            log.warning("Invalid coordinates!")

    tk.Button(tap_row, text="Tap", command=test_tap, font=(_FONT_FAMILY, 8)).pack(side=tk.LEFT)

    # ============================================================
    # STOP ALL / QUIT
    # ============================================================

    def stop_all():
        _stop_quest()
        _stop_titan()
        _stop_groot()
        _stop_pass_battle()
        _stop_occupy()
        _stop_reinforce_throne()
        _stop_mithril()
        for var in task_row_enabled_vars:
            var.set(False)
        for key in list(running_tasks.keys()):
            stop_task(key)
        log.info("=== ALL TASKS STOPPED ===")

    stop_frame = tk.Frame(window, bg="#333333", cursor="hand2")
    stop_label = tk.Label(stop_frame, text="STOP ALL", font=(_FONT_FAMILY, 11, "bold"),
                          bg="#333333", fg="white", pady=8)
    stop_label.pack(fill=tk.X)
    stop_frame.bind("<Button-1>", lambda e: stop_all())
    stop_label.bind("<Button-1>", lambda e: stop_all())
    stop_frame.pack(fill=tk.X, padx=PAD_X, pady=(6, 2))

    quit_row = tk.Frame(window, bg=COLOR_BG)
    quit_row.pack(pady=(4, 8))

    def restart():
        save_current_settings()
        stop_all()
        from updater import check_and_update
        check_and_update()
        # Launch new process and exit current one (closes old CMD window)
        window.destroy()
        subprocess.Popen([sys.executable] + sys.argv)
        sys.exit(0)

    def export_bug_report():
        """Collect logs, failure screenshots, stats, and settings into a zip file."""
        from botlog import stats, SCRIPT_DIR, LOG_DIR, STATS_DIR
        from datetime import datetime
        log = get_logger("main")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"pacbot_bugreport_{timestamp}.zip"

        save_path = filedialog.asksaveasfilename(
            defaultextension=".zip",
            filetypes=[("ZIP files", "*.zip")],
            initialfile=default_name,
            title="Save Bug Report",
        )
        if not save_path:
            return  # User cancelled

        try:
            with zipfile.ZipFile(save_path, "w", zipfile.ZIP_DEFLATED) as zf:
                # Logs (current + rotated backups)
                for suffix in ["", ".1", ".2", ".3"]:
                    logfile = os.path.join(LOG_DIR, f"pacbot.log{suffix}")
                    if os.path.isfile(logfile):
                        zf.write(logfile, f"logs/pacbot.log{suffix}")

                # Failure screenshots
                failures_dir = os.path.join(SCRIPT_DIR, "debug", "failures")
                if os.path.isdir(failures_dir):
                    for f in os.listdir(failures_dir):
                        if f.endswith(".png"):
                            zf.write(os.path.join(failures_dir, f), f"debug/failures/{f}")

                # Session stats
                if os.path.isdir(STATS_DIR):
                    for f in os.listdir(STATS_DIR):
                        if f.endswith(".json"):
                            zf.write(os.path.join(STATS_DIR, f), f"stats/{f}")

                # Settings
                settings_path = os.path.join(SCRIPT_DIR, "settings.json")
                if os.path.isfile(settings_path):
                    zf.write(settings_path, "settings.json")

                # System info report
                try:
                    from devices import get_devices
                    device_list = get_devices()
                except Exception:
                    device_list = ["(could not detect)"]

                info_lines = [
                    f"PACbot Bug Report",
                    f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    f"",
                    f"Version: {version}",
                    f"Python: {sys.version}",
                    f"OS: {platform.system()} {platform.release()} ({platform.version()})",
                    f"ADB: {config.adb_path}",
                    f"Devices: {', '.join(device_list) if device_list else '(none)'}",
                    f"",
                    f"=== Session Summary ===",
                    stats.summary(),
                ]
                zf.writestr("report_info.txt", "\n".join(info_lines))

            log.info("Bug report exported to %s", save_path)
            messagebox.showinfo("Bug Report", f"Bug report saved to:\n{save_path}")
        except Exception as e:
            log.error("Failed to export bug report: %s", e)
            messagebox.showerror("Bug Report", f"Failed to export bug report:\n{e}")

    tk.Button(quit_row, text="Restart", command=restart,
              font=(_FONT_FAMILY, 9), bg=COLOR_BG).pack(side=tk.LEFT, padx=(0, 8))
    tk.Button(quit_row, text="Bug Report", command=export_bug_report,
              font=(_FONT_FAMILY, 9), bg=COLOR_BG).pack(side=tk.LEFT, padx=(0, 8))
    tk.Button(quit_row, text="Quit", command=lambda: on_close(),
              font=(_FONT_FAMILY, 9), bg=COLOR_BG).pack(side=tk.LEFT)

    # ============================================================
    # PERIODIC CLEANUP
    # ============================================================

    def cleanup_dead_tasks():
        """Check for finished threads and clean up."""
        for key in list(running_tasks.keys()):
            info = running_tasks[key]
            if not isinstance(info, dict):
                continue
            thread = info.get("thread")
            if thread and not thread.is_alive():
                del running_tasks[key]

        if auto_quest_var.get() and not any(k.endswith("_auto_quest") for k in running_tasks):
            auto_quest_var.set(False)
            quest_frame.config(bg=COLOR_OFF)
            quest_label.config(text="Auto Quest: OFF", bg=COLOR_OFF)

        if auto_titan_var.get() and not any(k.endswith("_auto_titan") for k in running_tasks):
            auto_titan_var.set(False)
            titan_frame.config(bg=COLOR_OFF)
            titan_label.config(text="Rally Titans: OFF", bg=COLOR_OFF)

        if auto_groot_var.get() and not any(k.endswith("_auto_groot") for k in running_tasks):
            auto_groot_var.set(False)
            groot_frame.config(bg=COLOR_OFF)
            groot_label.config(text="Join Groot: OFF", bg=COLOR_OFF)

        if auto_pass_var.get() and not any(k.endswith("_auto_pass") for k in running_tasks):
            auto_pass_var.set(False)
            pass_frame.config(bg=COLOR_OFF)
            pass_label.config(text="Pass Battle: OFF", bg=COLOR_OFF)

        if auto_occupy_var.get() and not any(k.endswith("_auto_occupy") for k in running_tasks):
            auto_occupy_var.set(False)
            occupy_frame.config(bg=COLOR_OFF)
            occupy_label.config(text="Occupy Towers: OFF", bg=COLOR_OFF)

        if auto_reinforce_var.get() and not any(k.endswith("_auto_reinforce") for k in running_tasks):
            auto_reinforce_var.set(False)
            reinforce_frame.config(bg=COLOR_OFF)
            reinforce_label.config(text="Reinforce Throne: OFF", bg=COLOR_OFF)

        if auto_mithril_var.get() and not any(k.endswith("_auto_mithril") for k in running_tasks):
            auto_mithril_var.set(False)
            mithril_frame.config(bg=COLOR_OFF)
            mithril_label.config(text="Mine Mithril: OFF", bg=COLOR_OFF)
            config.MITHRIL_ENABLED = False
            config.MITHRIL_DEPLOY_TIME.clear()

        try:
            while True:
                alert = alert_queue.get_nowait()
                if alert == "no_marker":
                    messagebox.showwarning(
                        "Target Not Set",
                        'Target not set!\n\nPlease mark the pass or tower you are\ntargeting with a Personal "Enemy" marker.')
        except queue.Empty:
            pass

        window.after(3000, cleanup_dead_tasks)

    window.after(3000, cleanup_dead_tasks)

    def update_mithril_timer():
        """Update the mithril button text with elapsed time since deploy."""
        if auto_mithril_var.get() and config.MITHRIL_DEPLOY_TIME:
            earliest = min(config.MITHRIL_DEPLOY_TIME.values())
            elapsed = int(time.time() - earliest)
            mm, ss = divmod(elapsed, 60)
            mithril_label.config(text=f"Mine Mithril: ON ({mm:02d}:{ss:02d})",
                                 bg=COLOR_ON)
        window.after(1000, update_mithril_timer)

    window.after(1000, update_mithril_timer)

    def on_close():
        try:
            save_current_settings()
        except Exception as e:
            print(f"Failed to save settings: {e}")
        try:
            stop_all()
        except Exception as e:
            print(f"Failed to stop tasks: {e}")
        # Save session stats and print summary
        try:
            from botlog import stats, get_logger
            _log = get_logger("main")
            stats.save()
            _log.info("Session stats saved")
            summary = stats.summary()
            if summary:
                _log.info("Session stats:\n%s", summary)
        except Exception as e:
            print(f"Failed to save stats: {e}")
        # Flush all log handlers before exiting
        try:
            logging.shutdown()
        except Exception:
            pass
        try:
            window.destroy()
        except Exception:
            pass
        os._exit(0)

    window.protocol("WM_DELETE_WINDOW", on_close)

    window.update_idletasks()
    window.geometry(f"{WIN_WIDTH}x{window.winfo_reqheight()}")

    window.mainloop()

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    # Set up structured logging (rotating file + console)
    from botlog import setup_logging, stats, get_logger
    setup_logging()
    config.log_adb_path()

    # Compatibility bridge: capture any remaining print() calls to legacy log file
    _log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pacbot.log")
    _log_file = open(_log_path, "w", encoding="utf-8")

    class _Tee:
        """Write to both the original stream and the log file."""
        def __init__(self, stream, log):
            self._stream = stream
            self._log = log
        def write(self, data):
            self._stream.write(data)
            try:
                self._log.write(data)
                self._log.flush()
            except Exception:
                pass
        def flush(self):
            self._stream.flush()
            try:
                self._log.flush()
            except Exception:
                pass

    sys.stdout = _Tee(sys.stdout, _log_file)
    sys.stderr = _Tee(sys.stderr, _log_file)

    _main_log = get_logger("main")

    app_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isdir(os.path.join(app_dir, ".git")):
        from license import validate_license
        validate_license()
    else:
        _main_log.info("Git repo detected — skipping license check (developer mode).")
    # Auto-update check (skipped automatically for .git clones)
    from updater import check_and_update
    if check_and_update():
        _main_log.info("Update installed — restarting...")
        os.execv(sys.executable, [sys.executable] + sys.argv)

    _main_log.info("Running PACbot...")
    # Pre-initialize OCR engine in background thread (Windows only — loads EasyOCR models).
    # On macOS, Apple Vision has no startup cost so this is a no-op.
    threading.Thread(target=warmup_ocr, daemon=True).start()
    create_gui()
