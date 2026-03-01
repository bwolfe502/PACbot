"""PACbot Web Dashboard — mobile-friendly remote control via Flask.

Runs alongside the tkinter GUI in a background thread.  Both share the same
process, so they see the same ``config.running_tasks``, ``config.DEVICE_STATUS``,
and all task functions.

Enable via settings.json::

    "web_dashboard": true

Then access at ``http://<your-ip>:8080`` from any browser.
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
from config import (running_tasks, QuestType, RallyType, Screen)
from devices import get_devices, get_emulator_instances, auto_connect_emulators
from navigation import check_screen
from vision import (adb_tap, load_screenshot, find_image, tap_image,
                    wait_for_image_and_tap, read_ap, warmup_ocr)
from troops import troops_avail, heal_all, read_panel_statuses, get_troop_status
from actions import (attack, phantom_clash_attack, reinforce_throne, target,
                     check_quests, teleport, teleport_benchmark,
                     rally_titan, rally_eg,
                     search_eg_reset, join_rally, join_war_rallies,
                     reset_quest_tracking, reset_rally_blacklist,
                     mine_mithril, mine_mithril_if_due,
                     gather_gold, gather_gold_loop,
                     get_quest_tracking_state, occupy_tower)
from territory import attack_territory, diagnose_grid, scan_test_squares
from botlog import get_logger

try:
    from tunnel import tunnel_status
except ImportError:
    def tunnel_status():
        return "disabled"

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
    "Diagnose Grid": diagnose_grid,
    "Scan Corner Coords": scan_test_squares,
    "Test Teleport": lambda dev: teleport(dev, dry_run=True),
    "Teleport Benchmark": teleport_benchmark,
    "Mine Mithril": mine_mithril,
    "Gather Gold": gather_gold,
    "Reinforce Tower": occupy_tower,
}

# Auto-mode names grouped by category, per game mode
# Broken Lands: Combat first, then Farming
# Home Server: Events, Farming, Combat
AUTO_MODES_BL = [
    {"group": "Combat", "modes": [
        {"key": "auto_pass",      "label": "Pass Battle"},
        {"key": "auto_occupy",    "label": "Occupy Towers"},
        {"key": "auto_reinforce", "label": "Reinforce Throne"},
    ]},
    {"group": "Farming", "modes": [
        {"key": "auto_quest",     "label": "Auto Quest"},
        {"key": "auto_titan",     "label": "Rally Titans"},
        {"key": "auto_gold",      "label": "Mine Gold"},
        {"key": "auto_mithril",   "label": "Mine Mithril"},
    ]},
]

AUTO_MODES_HS = [
    {"group": "Events", "modes": [
        {"key": "auto_groot",     "label": "Join Groot"},
    ]},
    {"group": "Farming", "modes": [
        {"key": "auto_titan",     "label": "Rally Titans"},
        {"key": "auto_gold",      "label": "Mine Gold"},
        {"key": "auto_mithril",   "label": "Mine Mithril"},
    ]},
    {"group": "Combat", "modes": [
        {"key": "auto_reinforce", "label": "Reinforce Throne"},
    ]},
]

# One-shot action names (grouped for display)
ONESHOT_FARM = ["Rally Evil Guard", "Join Titan Rally", "Join Evil Guard Rally",
                "Join Groot Rally", "Heal All"]
ONESHOT_WAR = ["Target", "Attack", "Phantom Clash Attack", "Reinforce Throne",
               "UP UP UP!", "Teleport", "Attack Territory"]
ONESHOT_DEBUG = ["Check Screen", "Check Troops", "Diagnose Grid",
                 "Scan Corner Coords", "Test Teleport", "Teleport Benchmark"]

# ---------------------------------------------------------------------------
# Task runners (shared module — no more duplication)
# ---------------------------------------------------------------------------

from runners import (sleep_interval, run_auto_quest, run_auto_titan, run_auto_groot,
                     run_auto_pass, run_auto_occupy, run_auto_reinforce,
                     run_auto_mithril, run_auto_gold, run_once, run_repeat,
                     launch_task, stop_task, stop_all_tasks_matching)



# Map auto-mode keys to their runner functions
AUTO_RUNNERS = {
    "auto_quest":     lambda dev, se, s: run_auto_quest(dev, se),
    "auto_titan":     lambda dev, se, s: run_auto_titan(dev, se, s.get("titan_interval", 30), s.get("variation", 0)),
    "auto_groot":     lambda dev, se, s: run_auto_groot(dev, se, s.get("groot_interval", 30), s.get("variation", 0)),
    "auto_pass":      lambda dev, se, s: run_auto_pass(dev, se, s.get("pass_mode", "Rally Joiner"), s.get("pass_interval", 30), s.get("variation", 0)),
    "auto_occupy":    lambda dev, se, s: run_auto_occupy(dev, se),
    "auto_reinforce": lambda dev, se, s: run_auto_reinforce(dev, se, s.get("reinforce_interval", 30), s.get("variation", 0)),
    "auto_mithril":   lambda dev, se, s: run_auto_mithril(dev, se),
    "auto_gold":      lambda dev, se, s: run_auto_gold(dev, se),
}


# ---------------------------------------------------------------------------
# Dashboard-specific task helpers
# ---------------------------------------------------------------------------

def stop_all():
    """Stop every running task and clear device statuses."""
    # Reset loop-control flags that bypass stop events
    config.auto_occupy_running = False
    config.MITHRIL_ENABLED = False
    config.MITHRIL_DEPLOY_TIME.clear()
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

from settings import SETTINGS_FILE, DEFAULTS, load_settings as _load_settings, save_settings as _save_settings

from startup import apply_settings as _apply_settings


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


def ensure_firewall_open(port=8080):
    """On Windows, add a firewall rule to allow inbound TCP on *port*.

    Returns True if the rule was added (or already exists), False if we
    couldn't add it (e.g. not on Windows, or not running as admin).
    """
    if sys.platform != "win32":
        return True  # no firewall management needed

    import subprocess as _sp

    rule_name = f"PACbot Web Dashboard (TCP {port})"

    # Check if rule already exists
    try:
        check = _sp.run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             f"name={rule_name}"],
            capture_output=True, text=True, timeout=10,
        )
        if check.returncode == 0 and rule_name in check.stdout:
            _log.info("Firewall rule '%s' already exists", rule_name)
            return True
    except Exception:
        pass

    # Try to add the rule
    try:
        result = _sp.run(
            ["netsh", "advfirewall", "firewall", "add", "rule",
             f"name={rule_name}",
             "dir=in", "action=allow", "protocol=TCP",
             f"localport={port}", "profile=private"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            _log.info("Firewall rule added: %s", rule_name)
            return True
        else:
            _log.warning(
                "Could not add firewall rule (need admin?). "
                "Remote devices may not be able to connect.\n"
                "  Fix: run as Administrator, or manually allow TCP port %d:\n"
                '  netsh advfirewall firewall add rule name="%s" '
                "dir=in action=allow protocol=TCP localport=%d profile=private",
                port, rule_name, port,
            )
            return False
    except FileNotFoundError:
        _log.warning("netsh not found — cannot configure firewall automatically")
        return False
    except Exception as exc:
        _log.warning("Firewall rule creation failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Flask app factory
# ---------------------------------------------------------------------------

def create_app():
    template_dir = os.path.join(os.path.dirname(__file__), "templates")
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
    app.secret_key = os.urandom(24)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # no static file caching during dev
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    # --- Page routes ---

    @app.route("/")
    def index():
        cleanup_dead_tasks()
        devs, instances = _cached_devices()
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
        settings = _load_settings()
        mode = settings.get("mode", "bl")
        auto_groups = AUTO_MODES_BL if mode == "bl" else AUTO_MODES_HS
        # Build remote URL from auto-derived relay config
        relay_url = None
        from startup import get_relay_config
        relay_cfg = get_relay_config(settings)
        if relay_cfg:
            raw, _, bot_name = relay_cfg
            is_secure = raw.startswith("wss://")
            host = raw.replace("ws://", "").replace("wss://", "").split("/")[0]
            scheme = "https" if is_secure else "http"
            relay_url = f"{scheme}://{host}/{bot_name}"

        return render_template("index.html",
                               devices=device_info,
                               tasks=active_tasks,
                               task_count=len(active_tasks),
                               auto_groups=auto_groups,
                               mode=mode,
                               oneshot_farm=ONESHOT_FARM,
                               oneshot_war=ONESHOT_WAR,
                               active_tasks=active_tasks,
                               local_ip=get_local_ip(),
                               relay_url=relay_url)

    @app.route("/tasks")
    def tasks_page():
        return redirect(url_for("index"))

    @app.route("/settings")
    def settings_page():
        settings = _load_settings()
        # Build device_troops: merge saved values with currently detected devices
        saved_dt = settings.get("device_troops", {})
        detected, _ = _cached_devices()
        device_troops = {}
        for dev in detected:
            device_troops[dev] = saved_dt.get(dev, 5)
        # Also include saved devices not currently detected
        for dev, count in saved_dt.items():
            if dev not in device_troops:
                device_troops[dev] = count
        return render_template("settings.html", settings=settings,
                               device_troops=device_troops)

    @app.route("/debug")
    def debug_page():
        detected, _ = _cached_devices()
        device_info = [{"id": d, "name": d.split(":")[-1]} for d in detected]
        active_tasks = []
        for key, info in list(running_tasks.items()):
            thread = info.get("thread")
            if thread and thread.is_alive():
                active_tasks.append(key)
        log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
        lines = []
        log_file = os.path.join(log_dir, "pacbot.log")
        if os.path.isfile(log_file):
            try:
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-150:]
            except Exception:
                lines = ["(Could not read log file)"]
        return render_template("debug.html",
                               devices=device_info,
                               tasks=active_tasks,
                               debug_actions=ONESHOT_DEBUG,
                               log_lines=lines)

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

    # Cache device list to avoid spamming ADB on every poll
    _device_cache = {"devices": [], "instances": {}, "ts": 0}
    _DEVICE_CACHE_TTL = 15  # seconds

    def _cached_devices():
        now = time.time()
        if now - _device_cache["ts"] > _DEVICE_CACHE_TTL:
            _device_cache["devices"] = get_devices()
            _device_cache["instances"] = get_emulator_instances()
            _device_cache["ts"] = now
        return _device_cache["devices"], _device_cache["instances"]

    @app.route("/api/status")
    def api_status():
        cleanup_dead_tasks()
        devs, instances = _cached_devices()
        device_info = []
        for d in devs:
            # Troop snapshot (cached, no ADB call)
            snapshot = get_troop_status(d)
            troops_list = []
            snapshot_age = None
            if snapshot:
                snapshot_age = round(snapshot.age_seconds)
                for t in snapshot.troops:
                    troops_list.append({
                        "action": t.action.value,
                        "time_left": t.time_left,
                    })
            device_info.append({
                "id": d,
                "name": instances.get(d, d),
                "status": config.DEVICE_STATUS.get(d, "Idle"),
                "troops": troops_list,
                "snapshot_age": snapshot_age,
                "quests": get_quest_tracking_state(d),
            })
        active = []
        for key, info in list(running_tasks.items()):
            if isinstance(info, dict):
                thread = info.get("thread")
                if thread and thread.is_alive():
                    active.append(key)
        return jsonify({"devices": device_info, "tasks": active,
                        "tunnel": tunnel_status()})

    @app.route("/api/devices/refresh", methods=["POST"])
    def api_refresh_devices():
        auto_connect_emulators()
        _device_cache["ts"] = 0  # bust cache
        return redirect(url_for("index"))

    @app.route("/tasks/start", methods=["POST"])
    def start_task():
        device_raw = request.form.get("device", "")
        task_name = request.form.get("task_name")
        task_type = request.form.get("task_type", "oneshot")  # "auto" or "oneshot"

        # Support comma-separated device list (multi-select checkboxes)
        devices_to_run = [d.strip() for d in device_raw.split(",") if d.strip()]
        if not devices_to_run:
            return redirect(url_for("tasks_page"))

        settings = _load_settings()

        # Validate device IDs against known devices
        known = set(_cached_devices()[0])
        devices_to_run = [d for d in devices_to_run if d in known]
        if not devices_to_run:
            return redirect(url_for("tasks_page"))

        for device in devices_to_run:
            if task_type == "auto":
                # Start an auto-mode
                mode_key = task_name
                task_key = f"{device}_{mode_key}"
                if task_key in running_tasks:
                    info = running_tasks[task_key]
                    if isinstance(info, dict) and info.get("thread") and info["thread"].is_alive():
                        continue

                # Exclusivity: stop conflicting modes before starting
                EXCLUSIVE = {
                    "auto_quest": ["auto_gold"],
                    "auto_titan": ["auto_gold"],
                    "auto_gold":  ["auto_quest", "auto_titan"],
                }
                for conflict in EXCLUSIVE.get(mode_key, []):
                    ckey = f"{device}_{conflict}"
                    if ckey in running_tasks:
                        stop_task(ckey)

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

    @app.route("/tasks/stop-mode", methods=["POST"])
    def stop_mode_route():
        """Stop all running tasks for a given auto-mode (across all devices)."""
        mode_key = request.form.get("mode_key")
        if mode_key:
            # Reset loop-control flags for modes that use them
            if mode_key == "auto_occupy":
                config.auto_occupy_running = False
            elif mode_key == "auto_mithril":
                config.MITHRIL_ENABLED = False
                config.MITHRIL_DEPLOY_TIME.clear()
            suffix = f"_{mode_key}"
            for key in list(running_tasks.keys()):
                if key.endswith(suffix):
                    stop_task(key)
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
                     "eg_rally_own", "titan_rally_own", "web_dashboard", "gather_enabled",
                     "tower_quest_enabled", "remote_access"]:
            settings[key] = key in request.form

        for key in ["ap_gem_limit", "min_troops", "variation", "titan_interval",
                     "groot_interval", "reinforce_interval", "pass_interval",
                     "mithril_interval", "gather_mine_level", "gather_max_troops"]:
            val = request.form.get(key, "")
            if val.isdigit():
                settings[key] = int(val)

        for key in ["pass_mode", "my_team", "mode"]:
            val = request.form.get(key)
            if val is not None:
                settings[key] = val

        # Per-device troop counts (form fields named dt_<device_id>)
        dt = settings.get("device_troops", {})
        for form_key in request.form:
            if form_key.startswith("dt_"):
                dev_id = form_key[3:]  # strip "dt_" prefix
                val = request.form[form_key]
                if val.isdigit():
                    dt[dev_id] = int(val)
        settings["device_troops"] = dt

        from config import validate_settings
        settings, warnings = validate_settings(settings, DEFAULTS)
        for w in warnings:
            _log.warning("Settings (web save): %s", w)
        _apply_settings(settings)
        _save_settings(settings)
        return redirect(url_for("settings_page"))

    @app.route("/api/restart", methods=["POST"])
    def api_restart():
        """Save settings, stop all tasks, and restart the process."""
        _log.info("=== RESTART requested via web dashboard ===")
        _save_settings(_load_settings())
        stop_all()

        def _do_restart():
            time.sleep(0.5)  # let the HTTP response flush
            os.environ["PACBOT_RESTART"] = "1"  # skip opening new window
            os.execv(sys.executable, [sys.executable] + sys.argv)

        threading.Thread(target=_do_restart, daemon=True).start()
        return jsonify({"ok": True, "message": "Restarting..."})

    @app.route("/api/bug-report", methods=["POST"])
    def api_bug_report():
        from startup import create_bug_report_zip
        from flask import send_file
        import io
        zip_bytes, filename = create_bug_report_zip()
        return send_file(
            io.BytesIO(zip_bytes),
            mimetype="application/zip",
            as_attachment=True,
            download_name=filename,
        )

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

    # --- Territory grid manager ---

    @app.route("/territory")
    def territory_page():
        return render_template("territory.html")

    @app.route("/api/territory/grid")
    def api_territory_grid():
        return jsonify({
            "attack": [list(s) for s in config.MANUAL_ATTACK_SQUARES],
            "ignore": [list(s) for s in config.MANUAL_IGNORE_SQUARES],
            "throne": [[11, 11], [11, 12], [12, 11], [12, 12]],
        })

    @app.route("/api/territory/grid", methods=["POST"])
    def api_territory_grid_save():
        data = request.get_json()
        config.MANUAL_ATTACK_SQUARES = {tuple(s) for s in data.get("attack", [])}
        config.MANUAL_IGNORE_SQUARES = {tuple(s) for s in data.get("ignore", [])}
        return jsonify({"ok": True})

    # --- QR code generator ---

    @app.route("/api/screenshot")
    def api_screenshot():
        """Take a live screenshot from a device and return it as PNG."""
        device = request.args.get("device", "")
        if not device:
            return "Missing device parameter", 400
        known = set(_cached_devices()[0])
        if device not in known:
            return "Unknown device", 404
        import io
        import cv2
        from flask import send_file
        screen = load_screenshot(device)
        if screen is None:
            return "Screenshot failed (ADB error)", 500
        _, buf = cv2.imencode(".png", screen)
        as_attachment = bool(request.args.get("download"))
        return send_file(io.BytesIO(buf.tobytes()), mimetype="image/png",
                         as_attachment=as_attachment,
                         download_name=f"screenshot_{device.replace(':', '_')}.png")

    @app.route("/api/qr")
    def api_qr():
        url = request.args.get("url", "")
        if not url:
            return "Missing url parameter", 400
        import io
        import qrcode
        from flask import Response
        qr = qrcode.QRCode(box_size=12, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(buf.getvalue(), mimetype="image/png")

    return app
