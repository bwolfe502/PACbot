import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import time
import traceback
import os
import random
import json

import config
from updater import get_current_version
from config import set_min_troops, set_auto_heal, set_territory_config, running_tasks
from devices import get_devices, get_emulator_instances, auto_connect_emulators
from navigation import check_screen
from vision import adb_tap, tap_image, load_screenshot, find_image, wait_for_image_and_tap
from troops import troops_avail, heal_all
from actions import (attack, reinforce_throne, target, check_quests, teleport,
                     rally_titan, rally_eg, join_rally, join_war_rallies)
from territory import (attack_territory, auto_occupy_loop,
                       open_territory_manager, sample_specific_squares)

# ============================================================
# PERSISTENT SETTINGS
# ============================================================

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

DEFAULTS = {
    "auto_heal": True,
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
}

def load_settings():
    try:
        with open(SETTINGS_FILE, "r") as f:
            saved = json.load(f)
        return {**DEFAULTS, **saved}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULTS)

def save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        print(f"Failed to save settings: {e}")

# ============================================================
# FUNCTION LOOKUP
# ============================================================

TASK_FUNCTIONS = {
    "Rally Titan": rally_titan,
    "Rally Evil Guard": rally_eg,
    "Join Titan Rally": lambda dev: join_rally("titan", dev),
    "Join Evil Guard Rally": lambda dev: join_rally("eg", dev),
    "Join Groot Rally": lambda dev: join_rally("groot", dev),
    "Heal All": heal_all,
    "Target": target,
    "Attack": attack,
    "Reinforce Throne": reinforce_throne,
    "UP UP UP!": join_war_rallies,
    "Teleport": teleport,
    "Attack Territory": attack_territory,
    "Check Quests": check_quests,
    "Check Troops": troops_avail,
    "Check Screen": check_screen,
    "Sample Specific Squares": sample_specific_squares,
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
        print(f"    Waiting {actual}s (base {base} ±{variation})")
    for _ in range(actual):
        if stop_check():
            break
        time.sleep(1)

def run_auto_quest(device, stop_event):
    print(f"[{device}] Auto Quest started")
    stop_check = stop_event.is_set
    try:
        while not stop_check():
            troops = troops_avail(device)
            if troops > config.MIN_TROOPS_AVAILABLE:
                check_quests(device, stop_check=stop_check)
            else:
                print(f"[{device}] Not enough troops for quests")
            if stop_check():
                break
            for _ in range(10):
                if stop_check():
                    break
                time.sleep(1)
    except Exception as e:
        print(f"[{device}] ERROR in Auto Quest: {e}")
        traceback.print_exc()
    print(f"[{device}] Auto Quest stopped")

def run_auto_titan(device, stop_event, interval, variation):
    """Loop rally_titan on a configurable interval."""
    print(f"[{device}] Rally Titan started (interval: {interval}s ±{variation}s)")
    stop_check = stop_event.is_set
    try:
        while not stop_check():
            if config.AUTO_HEAL_ENABLED:
                heal_all(device)
            troops = troops_avail(device)
            if troops > config.MIN_TROOPS_AVAILABLE:
                rally_titan(device)
            else:
                print(f"[{device}] Not enough troops for Rally Titan")
            if stop_check():
                break
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        print(f"[{device}] ERROR in Rally Titan: {e}")
        traceback.print_exc()
    print(f"[{device}] Rally Titan stopped")

def run_auto_groot(device, stop_event, interval, variation):
    """Loop join_rally('groot') on a configurable interval."""
    print(f"[{device}] Rally Groot started (interval: {interval}s ±{variation}s)")
    stop_check = stop_event.is_set
    try:
        while not stop_check():
            if config.AUTO_HEAL_ENABLED:
                heal_all(device)
            troops = troops_avail(device)
            if troops > config.MIN_TROOPS_AVAILABLE:
                join_rally("groot", device)
            else:
                print(f"[{device}] Not enough troops for Rally Groot")
            if stop_check():
                break
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        print(f"[{device}] ERROR in Rally Groot: {e}")
        traceback.print_exc()
    print(f"[{device}] Rally Groot stopped")

def run_auto_occupy(device, stop_event):
    config.auto_occupy_running = True

    # Monitor stop event in background and set config flag when stopped
    def monitor():
        stop_event.wait()
        config.auto_occupy_running = False

    threading.Thread(target=monitor, daemon=True).start()
    auto_occupy_loop(device)
    print(f"[{device}] Auto Occupy stopped")

def run_auto_pass(device, stop_event, pass_mode, pass_interval, variation):
    stop_check = stop_event.is_set

    def _pass_attack(device):
        if config.AUTO_HEAL_ENABLED:
            heal_all(device)
        troops = troops_avail(device)
        if troops <= config.MIN_TROOPS_AVAILABLE:
            print(f"[{device}] Not enough troops for pass battle")
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
                print(f"[{device}] Found reinforce button - reinforcing")
                tap_image("reinforce_button.png", device, threshold=0.5)
                time.sleep(1)
                tap_image("depart.png", device)
                return "reinforce"

            if find_image(screen, "attack_button.png", threshold=0.7):
                if pass_mode == "Rally Starter":
                    print(f"[{device}] Found attack button - starting rally")
                    tap_image("rally_button.png", device, threshold=0.7)
                    time.sleep(1)
                    if not tap_image("depart.png", device):
                        wait_for_image_and_tap("depart.png", device, timeout=5)
                    return "rally_started"
                else:
                    print(f"[{device}] Found attack button - enemy owns it, closing menu")
                    adb_tap(device, 560, 675)
                    time.sleep(0.5)
                    return "attack"

            time.sleep(0.5)

        print(f"[{device}] Neither reinforce nor attack button found, closing menu")
        adb_tap(device, 560, 675)
        time.sleep(0.5)
        return False

    print(f"[{device}] Auto Pass Battle started (mode: {pass_mode})")
    try:
        while not stop_check():
            result = target(device)
            if result == "no_marker":
                print(f"[{device}] *** TARGET NOT SET! ***")
                print(f"[{device}] Please mark the pass or tower with a Personal 'Enemy' marker.")
                print(f"[{device}] Auto Pass Battle stopping.")
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
                print(f"[{device}] Rally started - looping back")
                time.sleep(2)
            elif action == "attack":
                print(f"[{device}] Enemy owns pass - joining war rallies continuously")
                while not stop_check():
                    troops = troops_avail(device)
                    if troops <= config.MIN_TROOPS_AVAILABLE:
                        print(f"[{device}] Not enough troops, waiting...")
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
        print(f"[{device}] ERROR in Auto Pass Battle: {e}")
        traceback.print_exc()
    print(f"[{device}] Auto Pass Battle stopped")

def run_auto_reinforce(device, stop_event, interval, variation):
    """Loop reinforce_throne on a configurable interval."""
    print(f"[{device}] Auto Reinforce Throne started (interval: {interval}s ±{variation}s)")
    stop_check = stop_event.is_set
    try:
        while not stop_check():
            reinforce_throne(device)
            if stop_check():
                break
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        print(f"[{device}] ERROR in Auto Reinforce Throne: {e}")
        traceback.print_exc()
    print(f"[{device}] Auto Reinforce Throne stopped")

def run_repeat(device, task_name, function, interval, variation, stop_event):
    stop_check = stop_event.is_set
    print(f"[{device}] Starting repeating task: {task_name}")
    try:
        while not stop_check():
            print(f"[{device}] Running {task_name}...")
            function(device)
            print(f"[{device}] {task_name} completed, waiting {interval}s...")
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        print(f"[{device}] ERROR in {task_name}: {e}")
        traceback.print_exc()
    print(f"[{device}] {task_name} stopped")

def run_once(device, task_name, function):
    print(f"[{device}] Running {task_name}...")
    try:
        function(device)
        print(f"[{device}] {task_name} completed")
    except Exception as e:
        print(f"[{device}] ERROR in {task_name}: {e}")
        traceback.print_exc()

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
    print(f"[{device}] Started {task_name}")

def stop_task(task_key):
    """Signal a task to stop via its threading.Event."""
    if task_key in running_tasks:
        info = running_tasks[task_key]
        if isinstance(info, dict) and "stop_event" in info:
            info["stop_event"].set()
            print(f"Stop signal sent for {task_key}")

def stop_all_tasks_matching(suffix):
    """Stop all tasks whose task_key ends with the given suffix."""
    for key in list(running_tasks.keys()):
        if key.endswith(suffix):
            stop_task(key)

# ============================================================
# GUI
# ============================================================

COLOR_ON = "#2e7d32"
COLOR_OFF = "#c0392b"
COLOR_BG = "#f0f0f0"
COLOR_SECTION_BG = "#e8e8e8"
WIN_WIDTH = 520

def make_toggle_bar(parent, text, font_spec, on_click):
    """Create a clean full-width toggle bar."""
    frame = tk.Frame(parent, bg=COLOR_OFF, cursor="hand2")
    label = tk.Label(frame, text=text, font=font_spec,
                     bg=COLOR_OFF, fg="white", pady=10)
    label.pack(fill=tk.X)
    frame.bind("<Button-1>", lambda e: on_click())
    label.bind("<Button-1>", lambda e: on_click())
    return frame, label

def create_gui():
    global devices

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
    tk.Label(title_frame, text=f"PACbot v{version}", font=("Segoe UI", 16, "bold"),
             bg=COLOR_BG).pack()
    tk.Label(title_frame, text="Made by Nine", font=("Segoe UI", 9), fg="#888",
             bg=COLOR_BG).pack()

    def open_tutorial():
        tutorial_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "TUTORIAL.txt")
        if os.path.isfile(tutorial_path):
            os.startfile(tutorial_path)
        else:
            messagebox.showinfo("Tutorial", "TUTORIAL.txt not found.")

    how_to_label = tk.Label(title_frame, text="How to Use", font=("Segoe UI", 9, "underline"),
                            fg="#0066cc", cursor="hand2", bg=COLOR_BG)
    how_to_label.pack(pady=(2, 0))
    how_to_label.bind("<Button-1>", lambda e: open_tutorial())

    # ── Devices ──
    device_frame = tk.LabelFrame(window, text="Devices", font=("Segoe UI", 9, "bold"),
                                  padx=8, pady=4, bg=COLOR_BG)
    device_frame.pack(fill=tk.X, padx=PAD_X, pady=(4, 4))

    device_list_frame = tk.Frame(device_frame, bg=COLOR_BG)
    device_list_frame.pack(fill=tk.X)

    device_checkboxes = {}
    device_checkbox_widgets = []

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
            display_name = instance_map.get(device, device)
            cb = tk.Checkbutton(device_list_frame, text=display_name,
                                variable=device_checkboxes[device], font=("Segoe UI", 9),
                                bg=COLOR_BG, activebackground=COLOR_BG)
            cb.pack(anchor='w', padx=4)
            device_checkbox_widgets.append(cb)

        if not devices:
            lbl = tk.Label(device_list_frame, text="No devices found. Try Auto-Connect.",
                           font=("Segoe UI", 8), fg="#999", bg=COLOR_BG)
            lbl.pack(pady=2)
            device_checkbox_widgets.append(lbl)

    refresh_device_list()

    btn_row = tk.Frame(device_frame, bg=COLOR_BG)
    btn_row.pack(pady=(2, 0))
    tk.Button(btn_row, text="Refresh", command=refresh_device_list,
              font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=4)
    # Auto-Connect button hidden (crashes on click) but function still available
    # tk.Button(btn_row, text="Auto-Connect",
    #           command=lambda: (auto_connect_emulators(), refresh_device_list()),
    #           font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=4)

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

    rw_mode_btn = tk.Button(mode_frame, text="Home Server", font=("Segoe UI", 10, "bold"),
                             bg=COLOR_MODE_INACTIVE, fg="#555", relief=tk.FLAT,
                             activebackground=COLOR_MODE_INACTIVE, cursor="hand2")
    rw_mode_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 2))

    bl_mode_btn = tk.Button(mode_frame, text="Broken Lands", font=("Segoe UI", 10, "bold"),
                             bg=COLOR_MODE_ACTIVE, fg="white", relief=tk.FLAT,
                             activebackground=COLOR_MODE_ACTIVE, cursor="hand2")
    bl_mode_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(2, 0))

    # ============================================================
    # AUTO MODES — split into BL and Rest Week groups
    # ============================================================

    auto_frame = tk.Frame(window, bg=COLOR_BG)
    auto_frame.pack(fill=tk.X, padx=PAD_X, pady=(4, 0))

    # -- State variables for all toggles --
    auto_quest_var = tk.BooleanVar(value=False)
    auto_titan_var = tk.BooleanVar(value=False)
    auto_groot_var = tk.BooleanVar(value=False)
    auto_pass_var = tk.BooleanVar(value=False)
    auto_occupy_var = tk.BooleanVar(value=False)
    auto_reinforce_var = tk.BooleanVar(value=False)

    titan_interval_var = tk.StringVar(value=str(settings["titan_interval"]))
    groot_interval_var = tk.StringVar(value=str(settings["groot_interval"]))
    reinforce_interval_var = tk.StringVar(value=str(settings["reinforce_interval"]))
    pass_mode_var = tk.StringVar(value=settings["pass_mode"])
    pass_interval_var = tk.StringVar(value=str(settings["pass_interval"]))

    # -- Sub-frames for each mode's toggles --
    bl_toggles_frame = tk.Frame(auto_frame, bg=COLOR_BG)
    rw_toggles_frame = tk.Frame(auto_frame, bg=COLOR_BG)

    # -- helpers to turn each off --
    def _stop_quest():
        if auto_quest_var.get():
            auto_quest_var.set(False)
            quest_frame.config(bg=COLOR_OFF)
            quest_label.config(text="Auto Quest: OFF", bg=COLOR_OFF)
            stop_all_tasks_matching("_auto_quest")
            print("Stopping Auto Quest on all devices")

    def _stop_titan():
        if auto_titan_var.get():
            auto_titan_var.set(False)
            titan_frame.config(bg=COLOR_OFF)
            titan_label.config(text="Auto Rally Titans: OFF", bg=COLOR_OFF)
            stop_all_tasks_matching("_auto_titan")
            print("Stopping Auto Rally Titans on all devices")

    def _stop_groot():
        if auto_groot_var.get():
            auto_groot_var.set(False)
            groot_frame.config(bg=COLOR_OFF)
            groot_label.config(text="Auto Join Groot Rallies: OFF", bg=COLOR_OFF)
            stop_all_tasks_matching("_auto_groot")
            print("Stopping Auto Join Groot Rallies on all devices")

    def _stop_pass_battle():
        if auto_pass_var.get():
            auto_pass_var.set(False)
            pass_frame.config(bg=COLOR_OFF)
            pass_label.config(text="Auto Pass Battle: OFF", bg=COLOR_OFF)
            stop_all_tasks_matching("_auto_pass")
            print("Stopping Auto Pass Battle on all devices")

    def _stop_occupy():
        if auto_occupy_var.get():
            auto_occupy_var.set(False)
            occupy_frame.config(bg=COLOR_OFF)
            occupy_label.config(text="Auto Occupy: OFF", bg=COLOR_OFF)
            config.auto_occupy_running = False
            stop_all_tasks_matching("_auto_occupy")
            print("Stopping Auto Occupy on all devices")

    def _stop_reinforce_throne():
        if auto_reinforce_var.get():
            auto_reinforce_var.set(False)
            reinforce_frame.config(bg=COLOR_OFF)
            reinforce_label.config(text="Auto Reinforce Throne: OFF", bg=COLOR_OFF)
            stop_all_tasks_matching("_auto_reinforce")
            print("Stopping Auto Reinforce Throne on all devices")

    # ── Broken Lands toggles ──

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
                    print(f"Started Auto Quest on {device}")
        else:
            _stop_quest()

    quest_frame, quest_label = make_toggle_bar(
        bl_toggles_frame, "Auto Quest: OFF", ("Segoe UI", 12, "bold"), toggle_auto_quest)
    quest_frame.pack(fill=tk.X, pady=(0, 3))

    def toggle_auto_pass():
        active_devices = get_active_devices()
        if not auto_pass_var.get():
            _stop_quest()
            _stop_occupy()
            auto_pass_var.set(True)
            pass_frame.config(bg=COLOR_ON)
            pass_label.config(text="Auto Pass Battle: ON", bg=COLOR_ON)
            for device in active_devices:
                task_key = f"{device}_auto_pass"
                if task_key not in running_tasks:
                    stop_event = threading.Event()
                    mode = pass_mode_var.get()
                    interval = int(pass_interval_var.get())
                    variation = int(variation_var.get())
                    launch_task(device, "auto_pass",
                                run_auto_pass, stop_event, args=(device, stop_event, mode, interval, variation))
                    print(f"Started Auto Pass Battle on {device}")
        else:
            _stop_pass_battle()

    pass_frame, pass_label = make_toggle_bar(
        bl_toggles_frame, "Auto Pass Battle: OFF", ("Segoe UI", 12, "bold"), toggle_auto_pass)
    pass_frame.pack(fill=tk.X, pady=(0, 3))

    def toggle_auto_occupy():
        active_devices = get_active_devices()
        if not auto_occupy_var.get():
            _stop_quest()
            _stop_pass_battle()
            auto_occupy_var.set(True)
            occupy_frame.config(bg=COLOR_ON)
            occupy_label.config(text="Auto Occupy: ON", bg=COLOR_ON)
            for device in active_devices:
                task_key = f"{device}_auto_occupy"
                if task_key not in running_tasks:
                    stop_event = threading.Event()
                    launch_task(device, "auto_occupy",
                                run_auto_occupy, stop_event, args=(device, stop_event))
                    print(f"Started Auto Occupy on {device}")
        else:
            _stop_occupy()

    occupy_frame, occupy_label = make_toggle_bar(
        bl_toggles_frame, "Auto Occupy: OFF", ("Segoe UI", 12, "bold"), toggle_auto_occupy)
    occupy_frame.pack(fill=tk.X)

    # ── Rest Week toggles ──

    def toggle_auto_titan():
        active_devices = get_active_devices()
        if not auto_titan_var.get():
            _stop_groot()
            auto_titan_var.set(True)
            titan_frame.config(bg=COLOR_ON)
            titan_label.config(text="Auto Rally Titans: ON", bg=COLOR_ON)
            for device in active_devices:
                task_key = f"{device}_auto_titan"
                if task_key not in running_tasks:
                    stop_event = threading.Event()
                    interval = int(titan_interval_var.get())
                    variation = int(variation_var.get())
                    launch_task(device, "auto_titan",
                                run_auto_titan, stop_event, args=(device, stop_event, interval, variation))
                    print(f"Started Rally Titan on {device}")
        else:
            _stop_titan()

    titan_frame, titan_label = make_toggle_bar(
        rw_toggles_frame, "Auto Rally Titans: OFF", ("Segoe UI", 12, "bold"), toggle_auto_titan)
    titan_frame.pack(fill=tk.X, pady=(0, 3))

    def toggle_auto_groot():
        active_devices = get_active_devices()
        if not auto_groot_var.get():
            _stop_titan()
            auto_groot_var.set(True)
            groot_frame.config(bg=COLOR_ON)
            groot_label.config(text="Auto Join Groot Rallies: ON", bg=COLOR_ON)
            for device in active_devices:
                task_key = f"{device}_auto_groot"
                if task_key not in running_tasks:
                    stop_event = threading.Event()
                    interval = int(groot_interval_var.get())
                    variation = int(variation_var.get())
                    launch_task(device, "auto_groot",
                                run_auto_groot, stop_event, args=(device, stop_event, interval, variation))
                    print(f"Started Rally Groot on {device}")
        else:
            _stop_groot()

    groot_frame, groot_label = make_toggle_bar(
        rw_toggles_frame, "Auto Join Groot Rallies: OFF", ("Segoe UI", 12, "bold"), toggle_auto_groot)
    groot_frame.pack(fill=tk.X, pady=(0, 3))

    def toggle_auto_reinforce():
        active_devices = get_active_devices()
        if not auto_reinforce_var.get():
            auto_reinforce_var.set(True)
            reinforce_frame.config(bg=COLOR_ON)
            reinforce_label.config(text="Auto Reinforce Throne: ON", bg=COLOR_ON)
            for device in active_devices:
                task_key = f"{device}_auto_reinforce"
                if task_key not in running_tasks:
                    stop_event = threading.Event()
                    interval = int(reinforce_interval_var.get())
                    variation = int(variation_var.get())
                    launch_task(device, "auto_reinforce",
                                run_auto_reinforce, stop_event, args=(device, stop_event, interval, variation))
                    print(f"Started Auto Reinforce Throne on {device}")
        else:
            _stop_reinforce_throne()

    reinforce_frame, reinforce_label = make_toggle_bar(
        rw_toggles_frame, "Auto Reinforce Throne: OFF", ("Segoe UI", 12, "bold"), toggle_auto_reinforce)
    reinforce_frame.pack(fill=tk.X)

    # ── Mode switching ──

    def switch_mode(new_mode):
        if mode_var.get() == new_mode:
            return

        # Stop all running tasks when switching modes
        _stop_quest()
        _stop_titan()
        _stop_groot()
        _stop_pass_battle()
        _stop_occupy()
        _stop_reinforce_throne()

        mode_var.set(new_mode)

        if new_mode == "rw":
            bl_toggles_frame.pack_forget()
            rw_toggles_frame.pack(fill=tk.X)
            rw_mode_btn.config(bg=COLOR_MODE_ACTIVE, fg="white",
                               activebackground=COLOR_MODE_ACTIVE)
            bl_mode_btn.config(bg=COLOR_MODE_INACTIVE, fg="#555",
                               activebackground=COLOR_MODE_INACTIVE)
            # Swap settings row 2
            bl_settings_row.pack_forget()
            rw_settings_row.pack(fill=tk.X, pady=(4, 0))
        else:
            rw_toggles_frame.pack_forget()
            bl_toggles_frame.pack(fill=tk.X)
            bl_mode_btn.config(bg=COLOR_MODE_ACTIVE, fg="white",
                               activebackground=COLOR_MODE_ACTIVE)
            rw_mode_btn.config(bg=COLOR_MODE_INACTIVE, fg="#555",
                               activebackground=COLOR_MODE_INACTIVE)
            # Swap settings row 2
            rw_settings_row.pack_forget()
            bl_settings_row.pack(fill=tk.X, pady=(4, 0))

        window.update_idletasks()
        window.geometry(f"{WIN_WIDTH}x{window.winfo_reqheight()}")

    rw_mode_btn.config(command=lambda: switch_mode("rw"))
    bl_mode_btn.config(command=lambda: switch_mode("bl"))

    # Start in BL mode
    bl_toggles_frame.pack(fill=tk.X)

    # ============================================================
    # SETTINGS BAR (compact, mode-aware)
    # ============================================================

    settings_frame = tk.Frame(window, bg=COLOR_SECTION_BG, padx=10, pady=6)
    settings_frame.pack(fill=tk.X, padx=PAD_X, pady=(6, 4))

    # Row 1: Auto Heal + Min Troops (always visible)
    row1 = tk.Frame(settings_frame, bg=COLOR_SECTION_BG)
    row1.pack(fill=tk.X)

    auto_heal_var = tk.BooleanVar(value=settings["auto_heal"])
    set_auto_heal(settings["auto_heal"])

    def toggle_auto_heal():
        set_auto_heal(auto_heal_var.get())
        save_current_settings()

    tk.Checkbutton(row1, text="Auto Heal", variable=auto_heal_var,
                   command=toggle_auto_heal, font=("Segoe UI", 9),
                   bg=COLOR_SECTION_BG, activebackground=COLOR_SECTION_BG).pack(side=tk.LEFT)

    tk.Frame(row1, width=20, bg=COLOR_SECTION_BG).pack(side=tk.LEFT)

    tk.Label(row1, text="Min Troops:", font=("Segoe UI", 9),
             bg=COLOR_SECTION_BG).pack(side=tk.LEFT)
    min_troops_var = tk.StringVar(value=str(settings["min_troops"]))
    set_min_troops(settings["min_troops"])
    tk.Entry(row1, textvariable=min_troops_var, width=6,
             font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(4, 4))

    def update_min_troops():
        try:
            set_min_troops(int(min_troops_var.get()))
            save_current_settings()
        except:
            pass

    tk.Button(row1, text="Set", command=update_min_troops,
              font=("Segoe UI", 8)).pack(side=tk.LEFT)

    tk.Frame(row1, width=20, bg=COLOR_SECTION_BG).pack(side=tk.LEFT)

    variation_var = tk.StringVar(value=str(settings["variation"]))
    tk.Label(row1, text="Randomize \u00b1", font=("Segoe UI", 9),
             bg=COLOR_SECTION_BG).pack(side=tk.LEFT)
    tk.Entry(row1, textvariable=variation_var, width=4, justify="center",
             font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(4, 1))
    tk.Label(row1, text="s", font=("Segoe UI", 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)

    # Row 2 (BL): Pass mode & interval
    bl_settings_row = tk.Frame(settings_frame, bg=COLOR_SECTION_BG)

    tk.Label(bl_settings_row, text="Pass", font=("Segoe UI", 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)
    ttk.Combobox(bl_settings_row, textvariable=pass_mode_var,
                 values=["Rally Joiner", "Rally Starter"],
                 width=12, state="readonly", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(4, 4))
    tk.Entry(bl_settings_row, textvariable=pass_interval_var, width=4, justify="center",
             font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 1))
    tk.Label(bl_settings_row, text="s", font=("Segoe UI", 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)

    # Row 2 (Rest Week): Titan interval + Groot interval
    rw_settings_row = tk.Frame(settings_frame, bg=COLOR_SECTION_BG)

    tk.Label(rw_settings_row, text="Titan every", font=("Segoe UI", 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)
    tk.Entry(rw_settings_row, textvariable=titan_interval_var, width=4, justify="center",
             font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(4, 1))
    tk.Label(rw_settings_row, text="s", font=("Segoe UI", 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)

    tk.Frame(rw_settings_row, width=16, bg=COLOR_SECTION_BG).pack(side=tk.LEFT)

    tk.Label(rw_settings_row, text="Groot every", font=("Segoe UI", 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)
    tk.Entry(rw_settings_row, textvariable=groot_interval_var, width=4, justify="center",
             font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(4, 1))
    tk.Label(rw_settings_row, text="s", font=("Segoe UI", 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)

    tk.Frame(rw_settings_row, width=16, bg=COLOR_SECTION_BG).pack(side=tk.LEFT)

    tk.Label(rw_settings_row, text="Reinforce every", font=("Segoe UI", 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)
    tk.Entry(rw_settings_row, textvariable=reinforce_interval_var, width=4, justify="center",
             font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(4, 1))
    tk.Label(rw_settings_row, text="s", font=("Segoe UI", 9),
             bg=COLOR_SECTION_BG, fg="#555").pack(side=tk.LEFT)

    # Start with BL mode, then switch if saved mode was different
    bl_settings_row.pack(fill=tk.X, pady=(4, 0))
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
                               command=toggle_territory, font=("Segoe UI", 9, "bold"),
                               relief=tk.FLAT, bg=COLOR_SECTION_BG, activebackground="#ddd")
    territory_btn.pack(fill=tk.X)

    # Territory content
    teams_row = tk.Frame(territory_inner, bg=COLOR_SECTION_BG)
    teams_row.pack(fill=tk.X, pady=2)
    tk.Label(teams_row, text="My Team:", font=("Segoe UI", 9),
             bg=COLOR_SECTION_BG).pack(side=tk.LEFT)
    my_team_var = tk.StringVar(value=settings["my_team"])
    ttk.Combobox(teams_row, textvariable=my_team_var,
                 values=["yellow", "red", "blue", "green"],
                 width=7, state="readonly", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(4, 12))
    tk.Label(teams_row, text="Attack:", font=("Segoe UI", 9),
             bg=COLOR_SECTION_BG).pack(side=tk.LEFT)
    enemy_var = tk.StringVar(value=settings["enemy_team"])
    ttk.Combobox(teams_row, textvariable=enemy_var,
                 values=["green", "red", "blue", "yellow"],
                 width=7, state="readonly", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(4, 6))

    set_territory_config(settings["my_team"], [settings["enemy_team"]])

    def update_territory_config():
        set_territory_config(my_team_var.get(), [enemy_var.get()])
        save_current_settings()

    tk.Button(teams_row, text="Set", command=update_territory_config,
              font=("Segoe UI", 8)).pack(side=tk.LEFT)

    def open_territory_mgr():
        active = get_active_devices()
        if active:
            threading.Thread(target=open_territory_manager, args=(active[0],), daemon=True).start()

    tk.Button(territory_inner, text="Territory Square Manager",
              command=open_territory_mgr, font=("Segoe UI", 9)).pack(fill=tk.X, pady=(4, 0))

    # ============================================================
    # SETTINGS PERSISTENCE
    # ============================================================

    def save_current_settings():
        save_settings({
            "auto_heal": auto_heal_var.get(),
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
                                    command=toggle_actions, font=("Segoe UI", 9, "bold"),
                                    relief=tk.FLAT, bg=COLOR_SECTION_BG, activebackground="#ddd")
    actions_toggle_btn.pack(fill=tk.X)

    # Tabs inside collapsible section
    tab_style = ttk.Style()
    tab_style.configure("Bold.TNotebook.Tab", font=("Segoe UI", 10, "bold"), padding=[12, 6])

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
                    print(f"Unknown function: {name}")
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
                print(f"Stopping {name}")
                for device in get_active_devices():
                    task_key = f"{device}_repeat:{name}"
                    stop_task(task_key)

        tk.Checkbutton(frame, variable=enabled, command=toggle).pack(side=tk.LEFT)
        tk.Entry(frame, textvariable=interval, width=4, justify="center",
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(2, 0))
        tk.Label(frame, text="s", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(1, 4))

        def do_run_once():
            enabled.set(False)
            func = TASK_FUNCTIONS.get(name)
            if not func:
                print(f"Unknown function: {name}")
                return
            for device in get_active_devices():
                repeat_key = f"{device}_repeat:{name}"
                stop_task(repeat_key)
                stop_event = threading.Event()
                launch_task(device, f"once:{name}",
                            run_once, stop_event, args=(device, name, func))

        tk.Button(frame, text=name, command=do_run_once,
                  font=("Segoe UI", 9)).pack(side=tk.LEFT, fill=tk.X, expand=True)

    def add_debug_button(parent, name, function):
        def do_run_once():
            for device in get_active_devices():
                threading.Thread(target=function, args=(device,), daemon=True).start()

        tk.Button(parent, text=name, command=do_run_once,
                  font=("Segoe UI", 9)).pack(pady=2, fill=tk.X)

    # Farm tab (Rally Titan removed — now a top-level toggle)
    add_task_row(farm_tab, "Rally Evil Guard", 30)
    add_task_row(farm_tab, "Join Titan Rally", 30)
    add_task_row(farm_tab, "Join Evil Guard Rally", 30)
    add_task_row(farm_tab, "Join Groot Rally", 30)
    add_task_row(farm_tab, "Heal All", 30)

    # War tab
    add_task_row(war_tab, "Target", 30)
    add_task_row(war_tab, "Attack", 30)
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
            print(f"[{device}] Screenshot saved to {path}")
        else:
            print(f"[{device}] Failed to take screenshot")

    add_debug_button(debug_tab, "Save Screenshot", save_screenshot)
    add_debug_button(debug_tab, "Check Quests", check_quests)
    add_debug_button(debug_tab, "Check Troops", troops_avail)
    add_debug_button(debug_tab, "Check Screen", check_screen)
    add_debug_button(debug_tab, "Attack Territory (Debug)", lambda dev: attack_territory(dev, debug=True))
    add_debug_button(debug_tab, "Sample Specific Squares", sample_specific_squares)

    tk.Frame(debug_tab, height=1, bg="gray80").pack(fill=tk.X, pady=6)

    tap_row = tk.Frame(debug_tab)
    tap_row.pack(fill=tk.X, pady=2)
    tk.Label(tap_row, text="Tap X:", font=("Segoe UI", 9)).pack(side=tk.LEFT)
    x_var = tk.StringVar()
    tk.Entry(tap_row, textvariable=x_var, width=5, font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(2, 8))
    tk.Label(tap_row, text="Y:", font=("Segoe UI", 9)).pack(side=tk.LEFT)
    y_var = tk.StringVar()
    tk.Entry(tap_row, textvariable=y_var, width=5, font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(2, 8))

    def test_tap():
        try:
            x, y = int(x_var.get()), int(y_var.get())
            for device in get_active_devices():
                adb_tap(device, x, y)
                print(f"[{device}] Test tapped ({x}, {y})")
        except ValueError:
            print("Invalid coordinates!")

    tk.Button(tap_row, text="Tap", command=test_tap, font=("Segoe UI", 8)).pack(side=tk.LEFT)

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
        for var in task_row_enabled_vars:
            var.set(False)
        for key in list(running_tasks.keys()):
            stop_task(key)
        print("=== ALL TASKS STOPPED ===")

    stop_frame = tk.Frame(window, bg="#333333", cursor="hand2")
    stop_label = tk.Label(stop_frame, text="STOP ALL", font=("Segoe UI", 11, "bold"),
                          bg="#333333", fg="white", pady=8)
    stop_label.pack(fill=tk.X)
    stop_frame.bind("<Button-1>", lambda e: stop_all())
    stop_label.bind("<Button-1>", lambda e: stop_all())
    stop_frame.pack(fill=tk.X, padx=PAD_X, pady=(6, 2))

    tk.Button(window, text="Quit", command=lambda: on_close(),
              font=("Segoe UI", 9), bg=COLOR_BG).pack(pady=(4, 8))

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
            titan_label.config(text="Auto Rally Titans: OFF", bg=COLOR_OFF)

        if auto_groot_var.get() and not any(k.endswith("_auto_groot") for k in running_tasks):
            auto_groot_var.set(False)
            groot_frame.config(bg=COLOR_OFF)
            groot_label.config(text="Auto Join Groot Rallies: OFF", bg=COLOR_OFF)

        if auto_pass_var.get() and not any(k.endswith("_auto_pass") for k in running_tasks):
            auto_pass_var.set(False)
            pass_frame.config(bg=COLOR_OFF)
            pass_label.config(text="Auto Pass Battle: OFF", bg=COLOR_OFF)

        if auto_occupy_var.get() and not any(k.endswith("_auto_occupy") for k in running_tasks):
            auto_occupy_var.set(False)
            occupy_frame.config(bg=COLOR_OFF)
            occupy_label.config(text="Auto Occupy: OFF", bg=COLOR_OFF)

        if auto_reinforce_var.get() and not any(k.endswith("_auto_reinforce") for k in running_tasks):
            auto_reinforce_var.set(False)
            reinforce_frame.config(bg=COLOR_OFF)
            reinforce_label.config(text="Auto Reinforce Throne: OFF", bg=COLOR_OFF)

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

    def on_close():
        try:
            save_current_settings()
            stop_all()
            window.destroy()
        except:
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
    from license import validate_license
    validate_license()
    print("Running PACbot...")
    create_gui()
