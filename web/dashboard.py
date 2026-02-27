"""PACbot Web Dashboard — mobile-friendly remote control via Flask.

Runs alongside the tkinter GUI in a background thread.  Both share the same
process, so they see the same ``config.running_tasks``, ``config.DEVICE_STATUS``,
and all task functions.

Enable via settings.json::

    "web_dashboard": true

Then access at ``http://<your-ip>:5000`` from any browser.
"""

import os
import sys
import glob
import json
import time
import random
import threading
import socket

from flask import Flask, render_template, request, redirect, url_for, jsonify

# ---------------------------------------------------------------------------
# PACbot imports (same as main.py)
# ---------------------------------------------------------------------------
import config
from config import (running_tasks, QuestType, RallyType, Screen,
                     set_min_troops, set_auto_heal, set_auto_restore_ap,
                     set_ap_restore_options, set_territory_config, set_eg_rally_own)
from devices import get_devices, get_emulator_instances, auto_connect_emulators
from navigation import check_screen
from vision import adb_tap, load_screenshot, read_ap, warmup_ocr
from troops import troops_avail, heal_all, read_panel_statuses
from actions import (attack, phantom_clash_attack, reinforce_throne, target,
                     check_quests, teleport, rally_titan, rally_eg,
                     search_eg_reset, join_rally, join_war_rallies,
                     reset_quest_tracking, reset_rally_blacklist,
                     mine_mithril, mine_mithril_if_due)
from territory import attack_territory, sample_specific_squares
from botlog import get_logger

_log = get_logger("web")

# ---------------------------------------------------------------------------
# Task functions (same map as main.py TASK_FUNCTIONS)
# ---------------------------------------------------------------------------

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

# Auto-mode names (ordered for display)
AUTO_MODES = [
    {"key": "auto_quest",     "label": "Auto Quest",       "has_interval": False},
    {"key": "auto_titan",     "label": "Rally Titans",     "has_interval": True, "setting": "titan_interval"},
    {"key": "auto_groot",     "label": "Join Groot",       "has_interval": True, "setting": "groot_interval"},
    {"key": "auto_pass",      "label": "Pass Battle",      "has_interval": True, "setting": "pass_interval"},
    {"key": "auto_occupy",    "label": "Occupy Towers",    "has_interval": False},
    {"key": "auto_reinforce", "label": "Reinforce Throne", "has_interval": True, "setting": "reinforce_interval"},
    {"key": "auto_mithril",   "label": "Mine Mithril",     "has_interval": False},
]

# One-shot action names (grouped for display)
ONESHOT_FARM = ["Rally Evil Guard", "Join Titan Rally", "Join Evil Guard Rally",
                "Join Groot Rally", "Heal All"]
ONESHOT_WAR = ["Target", "Attack", "Phantom Clash Attack", "Reinforce Throne",
               "UP UP UP!", "Teleport", "Attack Territory"]

# ---------------------------------------------------------------------------
# Task runner functions (duplicated from main.py to avoid circular imports)
# ---------------------------------------------------------------------------

def sleep_interval(base, variation, stop_check):
    actual = base + random.randint(-variation, variation) if variation > 0 else base
    actual = max(1, actual)
    for _ in range(actual):
        if stop_check():
            break
        time.sleep(1)

def _smart_wait_for_troops(device, stop_check, dlog, max_wait=120):
    snapshot = read_panel_statuses(device)
    if snapshot is None:
        return False
    soonest = snapshot.soonest_free()
    if soonest is None or soonest.time_left is None:
        return False
    wait_secs = soonest.time_left
    if wait_secs > max_wait:
        return False
    dlog.info("Troop %s finishes in %ds — waiting", soonest.action.value, wait_secs)
    for _ in range(wait_secs + 5):
        if stop_check():
            return False
        time.sleep(1)
    return True

def run_auto_quest(device, stop_event):
    dlog = get_logger("web", device)
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
                if not check_screen(device) == Screen.MAP:
                    from navigation import navigate
                    if not navigate(Screen.MAP, device):
                        dlog.warning("Cannot reach map — retrying in 10s")
                        config.set_device_status(device, "Navigating...")
                        time.sleep(10)
                        continue
                troops = troops_avail(device)
                if troops > config.MIN_TROOPS_AVAILABLE:
                    config.set_device_status(device, "Checking quests...")
                    check_quests(device, stop_check=stop_check)
                else:
                    dlog.warning("Not enough troops for quests")
                    config.set_device_status(device, "Waiting for troops...")
                    if _smart_wait_for_troops(device, stop_check, dlog):
                        continue
            if stop_check():
                break
            config.set_device_status(device, "Idle")
            for _ in range(10):
                if stop_check():
                    break
                time.sleep(1)
    except Exception as e:
        dlog.error("ERROR in Auto Quest: %s", e, exc_info=True)
    config.clear_device_status(device)

def run_auto_titan(device, stop_event, interval, variation):
    dlog = get_logger("web", device)
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
                from navigation import navigate
                if not navigate(Screen.MAP, device):
                    config.set_device_status(device, "Navigating...")
                    time.sleep(10)
                    continue
                troops = troops_avail(device)
                if troops > config.MIN_TROOPS_AVAILABLE:
                    if rally_count > 0 and rally_count % 5 == 0:
                        search_eg_reset(device)
                        if stop_check():
                            break
                    config.set_device_status(device, "Rallying titan...")
                    rally_titan(device)
                    rally_count += 1
                else:
                    config.set_device_status(device, "Waiting for troops...")
                    if _smart_wait_for_troops(device, stop_check, dlog):
                        continue
            if stop_check():
                break
            config.set_device_status(device, "Idle")
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        dlog.error("ERROR in Rally Titan: %s", e, exc_info=True)
    config.clear_device_status(device)

def run_auto_groot(device, stop_event, interval, variation):
    dlog = get_logger("web", device)
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
                from navigation import navigate
                if not navigate(Screen.MAP, device):
                    config.set_device_status(device, "Navigating...")
                    time.sleep(10)
                    continue
                troops = troops_avail(device)
                if troops > config.MIN_TROOPS_AVAILABLE:
                    config.set_device_status(device, "Joining groot rally...")
                    join_rally(RallyType.GROOT, device)
                else:
                    config.set_device_status(device, "Waiting for troops...")
                    if _smart_wait_for_troops(device, stop_check, dlog):
                        continue
            if stop_check():
                break
            config.set_device_status(device, "Idle")
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        dlog.error("ERROR in Rally Groot: %s", e, exc_info=True)
    config.clear_device_status(device)

def run_auto_pass(device, stop_event, pass_mode, interval, variation):
    dlog = get_logger("web", device)
    stop_check = stop_event.is_set
    lock = config.get_device_lock(device)
    try:
        while not stop_check():
            with lock:
                mine_mithril_if_due(device, stop_check=stop_check)
                if stop_check():
                    break
                config.set_device_status(device, "Pass battle...")
                result = target(device)
                if result == "no_marker":
                    dlog.warning("TARGET NOT SET — stopping")
                    break
                if stop_check() or not result:
                    break
                # Simplified pass attack
                if config.AUTO_HEAL_ENABLED:
                    heal_all(device)
                troops = troops_avail(device)
                if troops <= config.MIN_TROOPS_AVAILABLE:
                    config.set_device_status(device, "Waiting for troops...")
                    time.sleep(5)
                    continue
                adb_tap(device, 560, 675)
                time.sleep(2)
            if stop_check():
                break
            config.set_device_status(device, "Idle")
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        dlog.error("ERROR in Auto Pass: %s", e, exc_info=True)
    config.clear_device_status(device)

def run_auto_occupy(device, stop_event):
    from territory import auto_occupy_loop
    config.auto_occupy_running = True
    config.set_device_status(device, "Occupying towers...")
    def monitor():
        stop_event.wait()
        config.auto_occupy_running = False
    threading.Thread(target=monitor, daemon=True).start()
    auto_occupy_loop(device)
    config.clear_device_status(device)

def run_auto_reinforce(device, stop_event, interval, variation):
    dlog = get_logger("web", device)
    stop_check = stop_event.is_set
    lock = config.get_device_lock(device)
    try:
        while not stop_check():
            with lock:
                mine_mithril_if_due(device, stop_check=stop_check)
                if stop_check():
                    break
                config.set_device_status(device, "Reinforcing throne...")
                reinforce_throne(device)
            if stop_check():
                break
            config.set_device_status(device, "Idle")
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        dlog.error("ERROR in Auto Reinforce: %s", e, exc_info=True)
    config.clear_device_status(device)

def run_auto_mithril(device, stop_event):
    dlog = get_logger("web", device)
    stop_check = stop_event.is_set
    lock = config.get_device_lock(device)
    try:
        while not stop_check():
            with lock:
                config.set_device_status(device, "Mining mithril...")
                mine_mithril_if_due(device, stop_check=stop_check)
            if stop_check():
                break
            config.set_device_status(device, "Idle")
            sleep_interval(60, 0, stop_check)
    except Exception as e:
        dlog.error("ERROR in Auto Mithril: %s", e, exc_info=True)
    config.clear_device_status(device)

def run_once(device, task_name, function):
    dlog = get_logger("web", device)
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

def run_repeat(device, task_name, function, interval, variation, stop_event):
    dlog = get_logger("web", device)
    stop_check = stop_event.is_set
    lock = config.get_device_lock(device)
    try:
        while not stop_check():
            config.set_device_status(device, f"{task_name}...")
            with lock:
                function(device)
            config.set_device_status(device, "Idle")
            sleep_interval(interval, variation, stop_check)
    except Exception as e:
        dlog.error("ERROR in %s: %s", task_name, e, exc_info=True)
    config.clear_device_status(device)


# Map auto-mode keys to their runner functions
AUTO_RUNNERS = {
    "auto_quest":     lambda dev, se, s: run_auto_quest(dev, se),
    "auto_titan":     lambda dev, se, s: run_auto_titan(dev, se, s.get("titan_interval", 30), s.get("variation", 0)),
    "auto_groot":     lambda dev, se, s: run_auto_groot(dev, se, s.get("groot_interval", 30), s.get("variation", 0)),
    "auto_pass":      lambda dev, se, s: run_auto_pass(dev, se, s.get("pass_mode", "Rally Joiner"), s.get("pass_interval", 30), s.get("variation", 0)),
    "auto_occupy":    lambda dev, se, s: run_auto_occupy(dev, se),
    "auto_reinforce": lambda dev, se, s: run_auto_reinforce(dev, se, s.get("reinforce_interval", 30), s.get("variation", 0)),
    "auto_mithril":   lambda dev, se, s: run_auto_mithril(dev, se),
}


# ---------------------------------------------------------------------------
# Task launching / stopping (same pattern as main.py)
# ---------------------------------------------------------------------------

def launch_task(device, task_name, target_func, stop_event, args=()):
    thread = threading.Thread(target=target_func, args=args, daemon=True)
    thread.start()
    task_key = f"{device}_{task_name}"
    running_tasks[task_key] = {"thread": thread, "stop_event": stop_event}
    _log.info("Started %s on %s", task_name, device)

def stop_task(task_key):
    if task_key in running_tasks:
        info = running_tasks[task_key]
        if isinstance(info, dict) and "stop_event" in info:
            info["stop_event"].set()
            _log.debug("Stop signal sent for %s", task_key)

def stop_all():
    for key in list(running_tasks.keys()):
        stop_task(key)
    config.DEVICE_STATUS.clear()
    _log.info("=== ALL TASKS STOPPED (via web) ===")

def cleanup_dead_tasks():
    """Remove finished threads from running_tasks."""
    for key in list(running_tasks.keys()):
        info = running_tasks.get(key)
        if not isinstance(info, dict):
            continue
        thread = info.get("thread")
        if thread and not thread.is_alive():
            del running_tasks[key]


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "settings.json")

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
    "web_dashboard": False,
}

def _load_settings():
    try:
        with open(SETTINGS_FILE, "r") as f:
            saved = json.load(f)
        return {**DEFAULTS, **saved}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULTS)

def _save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        _log.error("Failed to save settings: %s", e)

def _apply_settings(settings):
    """Push settings values into config globals."""
    set_auto_heal(settings.get("auto_heal", True))
    set_auto_restore_ap(settings.get("auto_restore_ap", False))
    set_ap_restore_options(
        settings.get("ap_use_free", True),
        settings.get("ap_use_potions", True),
        settings.get("ap_allow_large_potions", True),
        settings.get("ap_use_gems", False),
        settings.get("ap_gem_limit", 0),
    )
    set_min_troops(settings.get("min_troops", 0))
    set_eg_rally_own(settings.get("eg_rally_own", True))
    set_territory_config(settings.get("my_team", "yellow"),
                         [settings.get("enemy_team", "green")])
    config.MITHRIL_INTERVAL = settings.get("mithril_interval", 19)
    from botlog import set_console_verbose
    set_console_verbose(settings.get("verbose_logging", False))


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def get_local_ip():
    """Best-effort detection of the machine's LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Flask app factory
# ---------------------------------------------------------------------------

def create_app():
    template_dir = os.path.join(os.path.dirname(__file__), "templates")
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
    app.secret_key = os.urandom(24)

    # --- Page routes ---

    @app.route("/")
    def index():
        cleanup_dead_tasks()
        devs = get_devices()
        instances = get_emulator_instances()
        device_info = []
        for d in devs:
            device_info.append({
                "id": d,
                "name": instances.get(d, d),
                "status": config.DEVICE_STATUS.get(d, "Idle"),
                "troops": config.DEVICE_TOTAL_TROOPS.get(d, 5),
            })
        active_tasks = []
        for key, info in list(running_tasks.items()):
            if isinstance(info, dict):
                thread = info.get("thread")
                if thread and thread.is_alive():
                    active_tasks.append(key)
        return render_template("index.html",
                               devices=device_info,
                               tasks=active_tasks,
                               task_count=len(active_tasks),
                               local_ip=get_local_ip())

    @app.route("/tasks")
    def tasks_page():
        cleanup_dead_tasks()
        devs = get_devices()
        instances = get_emulator_instances()
        device_list = [{"id": d, "name": instances.get(d, d)} for d in devs]
        settings = _load_settings()
        active = []
        for key, info in list(running_tasks.items()):
            if isinstance(info, dict):
                thread = info.get("thread")
                if thread and thread.is_alive():
                    active.append(key)
        return render_template("tasks.html",
                               devices=device_list,
                               auto_modes=AUTO_MODES,
                               oneshot_farm=ONESHOT_FARM,
                               oneshot_war=ONESHOT_WAR,
                               active_tasks=active,
                               settings=settings)

    @app.route("/settings")
    def settings_page():
        settings = _load_settings()
        return render_template("settings.html", settings=settings)

    @app.route("/logs")
    def logs_page():
        log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
        lines = []
        log_file = os.path.join(log_dir, "pacbot.log")
        if os.path.isfile(log_file):
            try:
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
                    lines = all_lines[-150:]
            except Exception:
                lines = ["(Could not read log file)"]
        return render_template("logs.html", lines=lines)

    # --- API routes ---

    @app.route("/api/status")
    def api_status():
        cleanup_dead_tasks()
        devs = get_devices()
        instances = get_emulator_instances()
        device_info = []
        for d in devs:
            device_info.append({
                "id": d,
                "name": instances.get(d, d),
                "status": config.DEVICE_STATUS.get(d, "Idle"),
            })
        active = []
        for key, info in list(running_tasks.items()):
            if isinstance(info, dict):
                thread = info.get("thread")
                if thread and thread.is_alive():
                    active.append(key)
        return jsonify({"devices": device_info, "tasks": active})

    @app.route("/api/devices/refresh", methods=["POST"])
    def api_refresh_devices():
        auto_connect_emulators()
        devs = get_devices()
        instances = get_emulator_instances()
        return jsonify({"devices": [{"id": d, "name": instances.get(d, d)} for d in devs]})

    @app.route("/tasks/start", methods=["POST"])
    def start_task():
        device = request.form.get("device")
        task_name = request.form.get("task_name")
        task_type = request.form.get("task_type", "oneshot")  # "auto" or "oneshot"

        if not device:
            return redirect(url_for("tasks_page"))

        settings = _load_settings()

        if task_type == "auto":
            # Start an auto-mode
            mode_key = task_name
            task_key = f"{device}_{mode_key}"
            if task_key in running_tasks:
                info = running_tasks[task_key]
                if isinstance(info, dict) and info.get("thread") and info["thread"].is_alive():
                    return redirect(url_for("tasks_page"))

            runner = AUTO_RUNNERS.get(mode_key)
            if runner:
                stop_event = threading.Event()
                if mode_key == "auto_mithril":
                    config.MITHRIL_ENABLED = True
                launch_task(device, mode_key,
                            lambda d=device, se=stop_event, s=settings: runner(d, se, s),
                            stop_event)
        else:
            # One-shot action
            func = TASK_FUNCTIONS.get(task_name)
            if func:
                stop_event = threading.Event()
                launch_task(device, f"once:{task_name}",
                            run_once, stop_event,
                            args=(device, task_name, func))

        return redirect(url_for("tasks_page"))

    @app.route("/tasks/stop", methods=["POST"])
    def stop_task_route():
        task_key = request.form.get("task_key")
        if task_key:
            stop_task(task_key)
        return redirect(url_for("tasks_page"))

    @app.route("/tasks/stop-all", methods=["POST"])
    def stop_all_route():
        stop_all()
        return redirect(url_for("tasks_page"))

    @app.route("/settings", methods=["POST"])
    def save_settings_route():
        settings = _load_settings()
        # Update from form
        for key in ["auto_heal", "auto_restore_ap", "ap_use_free", "ap_use_potions",
                     "ap_allow_large_potions", "ap_use_gems", "verbose_logging",
                     "eg_rally_own", "web_dashboard"]:
            settings[key] = key in request.form

        for key in ["ap_gem_limit", "min_troops", "variation", "titan_interval",
                     "groot_interval", "reinforce_interval", "pass_interval",
                     "mithril_interval"]:
            val = request.form.get(key, "")
            if val.isdigit():
                settings[key] = int(val)

        for key in ["pass_mode", "my_team", "enemy_team", "mode"]:
            val = request.form.get(key)
            if val:
                settings[key] = val

        _apply_settings(settings)
        _save_settings(settings)
        return redirect(url_for("settings_page"))

    @app.route("/api/logs")
    def api_logs():
        log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
        log_file = os.path.join(log_dir, "pacbot.log")
        lines = []
        if os.path.isfile(log_file):
            try:
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-150:]
            except Exception:
                pass
        return jsonify({"lines": [l.rstrip() for l in lines]})

    return app
