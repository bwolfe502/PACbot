import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import time
import traceback
import os

import config
from updater import get_current_version
from config import set_min_troops, set_auto_heal, set_territory_config, running_tasks
from devices import get_devices, get_emulator_instances, auto_connect_emulators
from navigation import check_screen
from vision import adb_tap, tap_image, load_screenshot, find_image, wait_for_image_and_tap
from troops import troops_avail, heal_all
from actions import (attack, target, check_quests, teleport,
                     rally_titan, rally_eg, join_rally, join_war_rallies)
from territory import (attack_territory, auto_occupy_loop,
                       open_territory_manager, sample_specific_squares)

# ============================================================
# FUNCTION LOOKUP
# ============================================================

TASK_FUNCTIONS = {
    "Rally Titan": rally_titan,
    "Rally Evil Guard": rally_eg,
    "Join Titan Rally": lambda dev: join_rally("titan", dev),
    "Join Evil Guard Rally": lambda dev: join_rally("eg", dev),
    "Heal All": heal_all,
    "Target": target,
    "Attack": attack,
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

def run_auto_occupy(device, stop_event):
    config.auto_occupy_running = True

    # Monitor stop event in background and set config flag when stopped
    def monitor():
        stop_event.wait()
        config.auto_occupy_running = False

    threading.Thread(target=monitor, daemon=True).start()
    auto_occupy_loop(device)
    print(f"[{device}] Auto Occupy stopped")

def run_auto_pass(device, stop_event, pass_mode, pass_interval):
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
                for _ in range(pass_interval):
                    if stop_check():
                        break
                    time.sleep(1)
            else:
                for _ in range(10):
                    if stop_check():
                        break
                    time.sleep(1)
    except Exception as e:
        print(f"[{device}] ERROR in Auto Pass Battle: {e}")
        traceback.print_exc()
    print(f"[{device}] Auto Pass Battle stopped")

def run_repeat(device, task_name, function, interval, stop_event):
    stop_check = stop_event.is_set
    print(f"[{device}] Starting repeating task: {task_name}")
    try:
        while not stop_check():
            print(f"[{device}] Running {task_name}...")
            function(device)
            print(f"[{device}] {task_name} completed, waiting {interval}s...")
            for _ in range(interval):
                if stop_check():
                    break
                time.sleep(1)
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

COLOR_ON = "#4CAF50"
COLOR_OFF = "#d9534f"

def make_toggle_label(parent, text, font_spec, on_click):
    """Create a clickable label that works as a colored button on macOS."""
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

    window = tk.Tk()
    window.title(f"PACbot v{version}")
    window.geometry("420x580")
    window.resizable(False, True)

    PAD_X = 20

    # ── Title ──
    title_frame = tk.Frame(window)
    title_frame.pack(fill=tk.X, pady=(8, 2))
    tk.Label(title_frame, text=f"PACbot v{version}", font=("Arial", 16, "bold")).pack()
    tk.Label(title_frame, text="Made by Nine", font=("Arial", 9), fg="gray").pack()

    def open_tutorial():
        tutorial_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "TUTORIAL.txt")
        if os.path.isfile(tutorial_path):
            os.startfile(tutorial_path)
        else:
            messagebox.showinfo("Tutorial", "TUTORIAL.txt not found.")

    tk.Button(title_frame, text="How to Use", command=open_tutorial,
              font=("Arial", 8), relief=tk.GROOVE).pack(pady=(2, 0))

    # ── Devices ──
    device_frame = tk.LabelFrame(window, text="Devices", font=("Arial", 10, "bold"),
                                  padx=8, pady=2)
    device_frame.pack(fill=tk.X, padx=PAD_X, pady=(4, 2))

    device_list_frame = tk.Frame(device_frame)
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
                                variable=device_checkboxes[device], font=("Arial", 10))
            cb.pack(anchor='w', padx=4)
            device_checkbox_widgets.append(cb)

        if not devices:
            lbl = tk.Label(device_list_frame, text="No devices found. Try Auto-Connect.",
                           font=("Arial", 9), fg="gray")
            lbl.pack(pady=2)
            device_checkbox_widgets.append(lbl)

    refresh_device_list()

    btn_row = tk.Frame(device_frame)
    btn_row.pack(pady=(2, 0))
    tk.Button(btn_row, text="Refresh", command=refresh_device_list).pack(side=tk.LEFT, padx=4)
    tk.Button(btn_row, text="Auto-Connect",
              command=lambda: (auto_connect_emulators(), refresh_device_list())
              ).pack(side=tk.LEFT, padx=4)

    def get_active_devices():
        return [d for d in devices if device_checkboxes.get(d, tk.BooleanVar(value=False)).get()]

    # ── Auto Quest & Auto Occupy toggle buttons (mutually exclusive) ──
    auto_frame = tk.Frame(window)
    auto_frame.pack(fill=tk.X, padx=PAD_X, pady=(8, 2))

    auto_quest_var = tk.BooleanVar(value=False)
    auto_occupy_var = tk.BooleanVar(value=False)
    auto_pass_var = tk.BooleanVar(value=False)

    # -- helpers to turn each off --
    def _stop_quest():
        if auto_quest_var.get():
            auto_quest_var.set(False)
            quest_frame.config(bg=COLOR_OFF)
            quest_label.config(text="Auto Quest: OFF", bg=COLOR_OFF)
            stop_all_tasks_matching("_auto_quest")
            print("Stopping Auto Quest on all devices")

    def _stop_occupy():
        if auto_occupy_var.get():
            auto_occupy_var.set(False)
            occupy_frame.config(bg=COLOR_OFF)
            occupy_label.config(text="Auto Occupy: OFF", bg=COLOR_OFF)
            config.auto_occupy_running = False
            stop_all_tasks_matching("_auto_occupy")
            print("Stopping Auto Occupy on all devices")

    def _stop_pass_battle():
        if auto_pass_var.get():
            auto_pass_var.set(False)
            pass_frame.config(bg=COLOR_OFF)
            pass_label.config(text="Auto Pass Battle: OFF", bg=COLOR_OFF)
            stop_all_tasks_matching("_auto_pass")
            print("Stopping Auto Pass Battle on all devices")

    # -- Auto Quest --
    def toggle_auto_quest():
        active_devices = get_active_devices()
        if not auto_quest_var.get():
            _stop_occupy()
            _stop_pass_battle()
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

    quest_frame, quest_label = make_toggle_label(
        auto_frame, "Auto Quest: OFF", ("Arial", 13, "bold"), toggle_auto_quest)
    quest_frame.pack(fill=tk.X, pady=(0, 4))

    # -- Auto Occupy --
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

    occupy_frame, occupy_label = make_toggle_label(
        auto_frame, "Auto Occupy: OFF", ("Arial", 13, "bold"), toggle_auto_occupy)
    occupy_frame.pack(fill=tk.X, pady=(0, 4))

    # -- Auto Pass Battle --
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
                    launch_task(device, "auto_pass",
                                run_auto_pass, stop_event, args=(device, stop_event, mode, interval))
                    print(f"Started Auto Pass Battle on {device}")
        else:
            _stop_pass_battle()

    pass_frame, pass_label = make_toggle_label(
        auto_frame, "Auto Pass Battle: OFF", ("Arial", 13, "bold"), toggle_auto_pass)
    pass_frame.pack(fill=tk.X)

    # ── Settings ──
    settings_frame = tk.LabelFrame(window, text="Settings", font=("Arial", 10, "bold"),
                                    padx=8, pady=4)
    settings_frame.pack(fill=tk.X, padx=PAD_X, pady=(8, 2))

    # Auto Heal
    auto_heal_var = tk.BooleanVar(value=True)
    set_auto_heal(True)

    def toggle_auto_heal():
        set_auto_heal(auto_heal_var.get())

    tk.Checkbutton(settings_frame, text="Auto Heal", variable=auto_heal_var,
                   command=toggle_auto_heal, font=("Arial", 10)).pack(anchor='w')

    # Pass Battle Mode
    pass_mode_row = tk.Frame(settings_frame)
    pass_mode_row.pack(fill=tk.X, pady=2)
    tk.Label(pass_mode_row, text="Pass Battle Mode:", font=("Arial", 10)).pack(side=tk.LEFT)
    pass_mode_var = tk.StringVar(value="Rally Joiner")
    ttk.Combobox(pass_mode_row, textvariable=pass_mode_var,
                 values=["Rally Joiner", "Rally Starter"],
                 width=12, state="readonly").pack(side=tk.LEFT, padx=(6, 0))

    # Pass Battle Interval
    pass_row = tk.Frame(settings_frame)
    pass_row.pack(fill=tk.X, pady=2)
    tk.Label(pass_row, text="Pass Battle Interval:", font=("Arial", 10)).pack(side=tk.LEFT)
    pass_interval_var = tk.StringVar(value="30")
    tk.Entry(pass_row, textvariable=pass_interval_var, width=4, justify="center").pack(side=tk.LEFT, padx=(6, 2))
    tk.Label(pass_row, text="s", font=("Arial", 10)).pack(side=tk.LEFT)

    # Min Troops
    troops_row = tk.Frame(settings_frame)
    troops_row.pack(fill=tk.X, pady=2)
    tk.Label(troops_row, text="Min Troops:", font=("Arial", 10)).pack(side=tk.LEFT)
    min_troops_var = tk.StringVar(value="0")
    tk.Entry(troops_row, textvariable=min_troops_var, width=6).pack(side=tk.LEFT, padx=(6, 4))

    def update_min_troops():
        try:
            set_min_troops(int(min_troops_var.get()))
        except:
            pass

    tk.Button(troops_row, text="Set", command=update_min_troops).pack(side=tk.LEFT)

    # Territory Teams
    teams_row = tk.Frame(settings_frame)
    teams_row.pack(fill=tk.X, pady=2)
    tk.Label(teams_row, text="My Team:", font=("Arial", 10)).pack(side=tk.LEFT)
    my_team_var = tk.StringVar(value="yellow")
    ttk.Combobox(teams_row, textvariable=my_team_var,
                 values=["yellow", "red", "blue", "green"],
                 width=7, state="readonly").pack(side=tk.LEFT, padx=(4, 12))
    tk.Label(teams_row, text="Attack:", font=("Arial", 10)).pack(side=tk.LEFT)
    enemy_var = tk.StringVar(value="green")
    ttk.Combobox(teams_row, textvariable=enemy_var,
                 values=["green", "red", "blue", "yellow"],
                 width=7, state="readonly").pack(side=tk.LEFT, padx=(4, 6))

    def update_territory_config():
        set_territory_config(my_team_var.get(), [enemy_var.get()])

    tk.Button(teams_row, text="Set", command=update_territory_config).pack(side=tk.LEFT)

    # Territory Square Manager
    def open_territory_mgr():
        active = get_active_devices()
        if active:
            threading.Thread(target=open_territory_manager, args=(active[0],), daemon=True).start()

    tk.Button(settings_frame, text="Territory Square Manager",
              command=open_territory_mgr).pack(fill=tk.X, pady=(4, 0))

    # ── Individual Actions (collapsed by default) ──
    actions_container = tk.Frame(window)
    actions_container.pack(fill=tk.BOTH, expand=True, padx=PAD_X, pady=(6, 0))

    actions_visible = tk.BooleanVar(value=False)
    actions_inner = tk.Frame(actions_container)

    def toggle_actions():
        if actions_visible.get():
            actions_inner.pack_forget()
            actions_visible.set(False)
            actions_toggle_btn.config(text="Individual Actions  \u25B6")
            window.update_idletasks()
            window.geometry(f"420x{window.winfo_reqheight()}")
        else:
            actions_inner.pack(fill=tk.BOTH, expand=True)
            actions_visible.set(True)
            actions_toggle_btn.config(text="Individual Actions  \u25BC")
            window.update_idletasks()
            window.geometry(f"420x{window.winfo_reqheight()}")

    actions_toggle_btn = tk.Button(actions_container, text="Individual Actions  \u25B6",
                                    command=toggle_actions, font=("Arial", 10, "bold"),
                                    relief=tk.GROOVE)
    actions_toggle_btn.pack(fill=tk.X)

    # Tabs inside collapsible section
    tabs = ttk.Notebook(actions_inner)
    tabs.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

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
                        launch_task(device, f"repeat:{name}",
                                    run_repeat, stop_event, args=(device, name, func, iv, stop_event))
            else:
                print(f"Stopping {name}")
                for device in get_active_devices():
                    task_key = f"{device}_repeat:{name}"
                    stop_task(task_key)

        tk.Checkbutton(frame, variable=enabled, command=toggle).pack(side=tk.LEFT)
        tk.Entry(frame, textvariable=interval, width=4, justify="center").pack(side=tk.LEFT, padx=(2, 0))
        tk.Label(frame, text="s").pack(side=tk.LEFT, padx=(1, 4))

        def do_run_once():
            enabled.set(False)  # stop any repeat loop
            func = TASK_FUNCTIONS.get(name)
            if not func:
                print(f"Unknown function: {name}")
                return
            for device in get_active_devices():
                # Stop repeating if running
                repeat_key = f"{device}_repeat:{name}"
                stop_task(repeat_key)
                # Launch one-shot as thread
                stop_event = threading.Event()
                launch_task(device, f"once:{name}",
                            run_once, stop_event, args=(device, name, func))

        tk.Button(frame, text=name, command=do_run_once).pack(side=tk.LEFT, fill=tk.X, expand=True)

    def add_debug_button(parent, name, function):
        def do_run_once():
            for device in get_active_devices():
                threading.Thread(target=function, args=(device,), daemon=True).start()

        tk.Button(parent, text=name, command=do_run_once).pack(pady=2, fill=tk.X)

    # Farm tab
    add_task_row(farm_tab, "Rally Titan", 30)
    add_task_row(farm_tab, "Rally Evil Guard", 30)
    add_task_row(farm_tab, "Join Titan Rally", 30)
    add_task_row(farm_tab, "Join Evil Guard Rally", 30)
    add_task_row(farm_tab, "Heal All", 30)

    # War tab
    add_task_row(war_tab, "Target", 30)
    add_task_row(war_tab, "Attack", 30)
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
    tk.Label(tap_row, text="Tap X:").pack(side=tk.LEFT)
    x_var = tk.StringVar()
    tk.Entry(tap_row, textvariable=x_var, width=5).pack(side=tk.LEFT, padx=(2, 8))
    tk.Label(tap_row, text="Y:").pack(side=tk.LEFT)
    y_var = tk.StringVar()
    tk.Entry(tap_row, textvariable=y_var, width=5).pack(side=tk.LEFT, padx=(2, 8))

    def test_tap():
        try:
            x, y = int(x_var.get()), int(y_var.get())
            for device in get_active_devices():
                adb_tap(device, x, y)
                print(f"[{device}] Test tapped ({x}, {y})")
        except ValueError:
            print("Invalid coordinates!")

    tk.Button(tap_row, text="Tap", command=test_tap).pack(side=tk.LEFT)

    # ── Stop All / Quit ──
    def stop_all():
        _stop_quest()
        _stop_occupy()
        _stop_pass_battle()
        # Stop all task row threads
        for var in task_row_enabled_vars:
            var.set(False)
        for key in list(running_tasks.keys()):
            stop_task(key)
        print("=== ALL TASKS STOPPED ===")

    stop_frame = tk.Frame(window, bg="#333333", cursor="hand2")
    stop_label = tk.Label(stop_frame, text="\u25A0  STOP ALL", font=("Arial", 12, "bold"),
                          bg="#333333", fg="white", pady=6)
    stop_label.pack(fill=tk.X)
    stop_frame.bind("<Button-1>", lambda e: stop_all())
    stop_label.bind("<Button-1>", lambda e: stop_all())
    stop_frame.pack(fill=tk.X, padx=PAD_X, pady=(6, 2))

    tk.Button(window, text="Quit", command=lambda: on_close()).pack(pady=6)

    # ── Periodic cleanup ──
    def cleanup_dead_tasks():
        """Check for finished threads and clean up."""
        for key in list(running_tasks.keys()):
            info = running_tasks[key]
            if not isinstance(info, dict):
                continue
            thread = info.get("thread")
            if thread and not thread.is_alive():
                del running_tasks[key]

        # Update toggle states if all threads of a type have exited
        if auto_quest_var.get() and not any(k.endswith("_auto_quest") for k in running_tasks):
            auto_quest_var.set(False)
            quest_frame.config(bg=COLOR_OFF)
            quest_label.config(text="Auto Quest: OFF", bg=COLOR_OFF)

        if auto_occupy_var.get() and not any(k.endswith("_auto_occupy") for k in running_tasks):
            auto_occupy_var.set(False)
            occupy_frame.config(bg=COLOR_OFF)
            occupy_label.config(text="Auto Occupy: OFF", bg=COLOR_OFF)

        if auto_pass_var.get() and not any(k.endswith("_auto_pass") for k in running_tasks):
            auto_pass_var.set(False)
            pass_frame.config(bg=COLOR_OFF)
            pass_label.config(text="Auto Pass Battle: OFF", bg=COLOR_OFF)

        # Check for alerts from tasks
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

    # ── Window close handler ──
    def on_close():
        stop_all()
        window.destroy()

    window.protocol("WM_DELETE_WINDOW", on_close)

    # Auto-size window height to fit all content
    window.update_idletasks()
    window.geometry(f"420x{window.winfo_reqheight()}")

    window.mainloop()

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("Running PACbot...")
    create_gui()
