import tkinter as tk
from tkinter import messagebox, filedialog
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
import zipfile
import customtkinter as ctk

import config
from updater import get_current_version
from config import (set_min_troops, set_auto_heal, set_auto_restore_ap,
                     set_ap_restore_options, set_territory_config, set_eg_rally_own,
                     set_titan_rally_own, set_gather_options, set_tower_quest_enabled,
                     running_tasks, QuestType, RallyType, Screen)
from devices import get_devices, get_emulator_instances, auto_connect_emulators
from navigation import check_screen, navigate
from vision import adb_tap, tap_image, load_screenshot, find_image, wait_for_image_and_tap, read_ap, warmup_ocr
from troops import troops_avail, heal_all, read_panel_statuses, get_troop_status, TroopAction
from actions import (attack, phantom_clash_attack, reinforce_throne, target, check_quests, teleport,
                     teleport_benchmark,
                     rally_titan, rally_eg, search_eg_reset, join_rally,
                     join_war_rallies, reset_quest_tracking, reset_rally_blacklist,
                     test_eg_positions, mine_mithril, mine_mithril_if_due,
                     gather_gold, gather_gold_loop, occupy_tower,
                     get_quest_tracking_state)
from territory import (attack_territory, auto_occupy_loop,
                       open_territory_manager, diagnose_grid)
from botlog import get_logger
from settings import SETTINGS_FILE, DEFAULTS, load_settings, save_settings
from runners import (sleep_interval, run_auto_quest, run_auto_titan, run_auto_groot,
                     run_auto_pass, run_auto_occupy, run_auto_reinforce,
                     run_auto_mithril, run_auto_gold, run_repeat, run_once,
                     launch_task, stop_task, stop_all_tasks_matching)

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
    "Diagnose Grid": diagnose_grid,
    "Mine Mithril": mine_mithril,
    "Gather Gold": gather_gold,
    "Reinforce Tower": occupy_tower,
}

# Alert queue lives in config.py — consumed by GUI, produced by runners
from config import alert_queue

devices = []
# ============================================================
# GUI
# ============================================================

WIN_WIDTH = 520

# Cross-platform font: "Segoe UI" on Windows, system default on macOS/Linux
_FONT_FAMILY = "Segoe UI" if platform.system() == "Windows" else "Helvetica Neue"

THEME = {
    "bg_deep":       "#0c0c18",
    "bg_card":       "#181830",
    "bg_section":    "#14142a",
    "bg_input":      "#141428",
    "bg_hover":      "#1e3a5f",
    "border_subtle": "#1a1a30",
    "border_cyan":   "#1a3a4a",
    "accent_cyan":   "#64d8ff",
    "accent_green":  "#4caf50",
    "accent_red":    "#ef5350",
    "accent_amber":  "#ffb74d",
    "text_primary":  "#e0e0f0",
    "text_secondary":"#8899aa",
    "text_muted":    "#667788",
    "text_label":    "#ccccdd",
    "text_white":    "#ffffff",
    "toggle_off":    "#252540",
    "toggle_on":     "#1b5e20",
    "knob_off":      "#556677",
    "knob_on":       "#4caf50",
    "mode_active":   "#1a5276",
    "mode_inactive": "#252540",
    "btn_default":   "#1e3a5f",
    "btn_danger":    "#c62828",
    "btn_danger_hover": "#d32f2f",
}

# Troop action → (bg_color, text_color) matching web dashboard CSS
TROOP_PILL_COLORS = {
    TroopAction.HOME:        ("#222238", "#8888aa"),
    TroopAction.RETURNING:   ("#1a3520", "#66bb6a"),
    TroopAction.RALLYING:    ("#1a3535", "#4dd9c0"),
    TroopAction.DEFENDING:   ("#2a1a3a", "#b388ff"),
    TroopAction.MARCHING:    ("#33301a", "#ffe082"),
    TroopAction.GATHERING:   ("#2e2510", "#c9a030"),
    TroopAction.OCCUPYING:   ("#1a2540", "#64b5f6"),
    TroopAction.STATIONING:  ("#1a2540", "#64b5f6"),
    TroopAction.BATTLING:    ("#3a1a1a", "#ef5350"),
    TroopAction.ADVENTURING: ("#1a2540", "#64b5f6"),
}

QUEST_LABELS = {
    "QuestType.TITAN": "Titans", "QuestType.EVIL_GUARD": "Evil Guard",
    "QuestType.PVP": "PvP", "QuestType.GATHER": "Gather",
    "QuestType.FORTRESS": "Fortress", "QuestType.TOWER": "Towers",
}

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

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")

    window = ctk.CTk()
    window.title(f"PACbot v{version}")
    window.resizable(False, True)
    window.configure(fg_color=THEME["bg_deep"])

    # Set window icon (title bar + taskbar)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if platform.system() == "Darwin":
        icon_png = os.path.join(script_dir, "icon.png")
        if os.path.isfile(icon_png):
            icon_img = tk.PhotoImage(file=icon_png)
            window.iconphoto(True, icon_img)
    else:
        icon_path = os.path.join(script_dir, "icon.ico")
        if os.path.isfile(icon_path):
            window.iconbitmap(icon_path)

    PAD_X = 16

    def _resize_window():
        """Resize window to fit content, correcting for CTk widget height inflation.

        CustomTkinter widgets use internal canvases that report ~60% more
        reqheight than their visible content.  Multiplying winfo_reqheight()
        by 0.68 gives a close approximation of the actual needed height
        while keeping the bottom bar (Restart/Bug Report/Quit) visible.
        """
        window.update_idletasks()
        req = window.winfo_reqheight()
        window.geometry(f"{WIN_WIDTH}x{max(int(req * 0.68), 460)}")

    # Shared style kwargs
    _cb_kw = dict(font=ctk.CTkFont(family=_FONT_FAMILY, size=12),
                  text_color=THEME["text_primary"],
                  fg_color=THEME["accent_cyan"], hover_color=THEME["bg_hover"],
                  border_color=THEME["border_subtle"],
                  checkmark_color=THEME["bg_deep"], corner_radius=4)
    _entry_kw = dict(font=ctk.CTkFont(family=_FONT_FAMILY, size=12),
                     fg_color=THEME["bg_input"], border_color=THEME["border_subtle"],
                     text_color=THEME["text_primary"], corner_radius=6, height=28)
    _sw_kw = dict(font=ctk.CTkFont(family=_FONT_FAMILY, size=12),
                  text_color=THEME["text_label"],
                  fg_color=THEME["toggle_off"], progress_color=THEME["toggle_on"],
                  button_color=THEME["knob_off"], button_hover_color=THEME["knob_on"],
                  switch_width=38, switch_height=20)
    _om_kw = dict(height=24,
                  font=ctk.CTkFont(family=_FONT_FAMILY, size=11),
                  fg_color=THEME["bg_input"], button_color=THEME["border_subtle"],
                  button_hover_color=THEME["bg_hover"],
                  dropdown_fg_color=THEME["bg_card"],
                  dropdown_hover_color=THEME["bg_hover"],
                  text_color=THEME["text_primary"], corner_radius=6)

    # ── Title ──
    title_frame = tk.Frame(window, bg=THEME["bg_deep"])
    title_frame.pack(fill=tk.X, pady=(10, 4))
    ctk.CTkLabel(title_frame, text=f"PACbot v{version}",
                 font=ctk.CTkFont(family=_FONT_FAMILY, size=18, weight="bold"),
                 text_color=THEME["accent_cyan"]).pack()
    ctk.CTkLabel(title_frame, text="Made by Nine",
                 font=ctk.CTkFont(family=_FONT_FAMILY, size=10),
                 text_color=THEME["text_muted"]).pack()

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

    links_row = tk.Frame(title_frame, bg=THEME["bg_deep"])
    links_row.pack(pady=(2, 0))
    ctk.CTkButton(links_row, text="How to Use",
                  font=ctk.CTkFont(family=_FONT_FAMILY, size=10, underline=True),
                  fg_color="transparent", hover_color=THEME["bg_hover"],
                  text_color=THEME["accent_cyan"], width=80, height=20,
                  command=open_tutorial).pack(side=tk.LEFT, padx=4)

    # _web_open_url is set later — defaults to local, overridden by relay
    _web_open_url = [None]

    def _open_web_link():
        import webbrowser
        if _web_open_url[0]:
            webbrowser.open(_web_open_url[0])

    web_link_btn = ctk.CTkButton(links_row, text="Web App",
                  font=ctk.CTkFont(family=_FONT_FAMILY, size=10, underline=True),
                  fg_color="transparent", hover_color=THEME["bg_hover"],
                  text_color=THEME["accent_cyan"], width=80, height=20,
                  command=_open_web_link)
    if settings.get("web_dashboard", False):
        web_link_btn.pack(side=tk.LEFT, padx=4)

    # ── Devices ──
    devices_container = tk.Frame(window, bg=THEME["bg_deep"])
    devices_container.pack(fill=tk.X, padx=PAD_X, pady=(4, 4))

    device_checkboxes = {}
    device_card_widgets = []  # top-level widgets to destroy on refresh
    device_troops_vars = {}   # {device_id: StringVar} for per-device total troops
    # Per-device display refs: {device_id: {status_label, troop_frame, quest_frame}}
    device_display = {}

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

        for widget in device_card_widgets:
            widget.destroy()
        device_card_widgets.clear()
        device_display.clear()

        for device in devices:
            if device not in device_checkboxes:
                device_checkboxes[device] = tk.BooleanVar(value=True)

            # Per-device total troops (default 5, restore from saved settings)
            if device not in device_troops_vars:
                saved_val = saved_device_troops.get(device, 5)
                device_troops_vars[device] = tk.StringVar(value=str(saved_val))
                config.DEVICE_TOTAL_TROOPS[device] = saved_val

            display_name = instance_map.get(device, device)

            # ── Per-device card (tk.Frame for correct auto-sizing) ──
            card = tk.Frame(devices_container, bg=THEME["bg_card"],
                            highlightbackground=THEME["border_subtle"],
                            highlightthickness=1)
            card.pack(fill=tk.X, pady=(0, 4))
            device_card_widgets.append(card)

            # Header row: checkbox + name + troops selector
            header = tk.Frame(card, bg=THEME["bg_card"])
            header.pack(fill=tk.X, padx=10, pady=(6, 0))

            cb = ctk.CTkCheckBox(header, text=display_name,
                                 variable=device_checkboxes[device], **_cb_kw)
            cb.pack(side=tk.LEFT)

            ctk.CTkLabel(header, text="troops:",
                         font=ctk.CTkFont(family=_FONT_FAMILY, size=10),
                         text_color=THEME["text_muted"]).pack(side=tk.RIGHT, padx=(4, 0))
            troops_om = ctk.CTkOptionMenu(
                header, variable=device_troops_vars[device],
                values=["1", "2", "3", "4", "5"],
                command=lambda _: (_apply_device_troops(), save_current_settings()),
                width=50, height=22,
                font=ctk.CTkFont(family=_FONT_FAMILY, size=10),
                fg_color=THEME["bg_input"], button_color=THEME["border_subtle"],
                button_hover_color=THEME["bg_hover"],
                dropdown_fg_color=THEME["bg_card"],
                dropdown_hover_color=THEME["bg_hover"],
                text_color=THEME["text_primary"], corner_radius=4)
            troops_om.pack(side=tk.RIGHT)

            # Status bar
            status_lbl = ctk.CTkLabel(card, text="Idle",
                                      font=ctk.CTkFont(family=_FONT_FAMILY, size=11),
                                      text_color=THEME["text_muted"],
                                      fg_color=THEME["bg_card"])
            status_lbl.pack(anchor=tk.W, padx=12, pady=(2, 0))

            # Troop pills row
            troop_frame = tk.Frame(card, bg=THEME["bg_card"])
            troop_frame.pack(fill=tk.X, padx=10, pady=(2, 0))

            # Quest pills row
            quest_frame = tk.Frame(card, bg=THEME["bg_card"])
            quest_frame.pack(fill=tk.X, padx=10, pady=(0, 6))

            device_display[device] = {
                "status_label": status_lbl,
                "troop_frame": troop_frame,
                "quest_frame": quest_frame,
            }

        _apply_device_troops()

        if not devices:
            lbl = ctk.CTkLabel(devices_container,
                               text="No devices found. Start your emulator and click Refresh.",
                               font=ctk.CTkFont(family=_FONT_FAMILY, size=10),
                               text_color=THEME["text_muted"])
            lbl.pack(pady=2)
            device_card_widgets.append(lbl)

    auto_connect_emulators()
    refresh_device_list()

    refresh_row = tk.Frame(devices_container, bg=THEME["bg_deep"])
    refresh_row.pack()
    device_card_widgets_static = []  # not cleared on refresh
    ctk.CTkButton(refresh_row, text="Refresh Devices",
                  command=lambda: (auto_connect_emulators(), refresh_device_list()),
                  font=ctk.CTkFont(family=_FONT_FAMILY, size=10),
                  fg_color=THEME["btn_default"], hover_color=THEME["bg_hover"],
                  corner_radius=8, height=26, width=120).pack(padx=4)

    def get_active_devices():
        return [d for d in devices if device_checkboxes.get(d, tk.BooleanVar(value=False)).get()]

    # ============================================================
    # MODE TOGGLE — CTkSegmentedButton
    # ============================================================

    mode_var = tk.StringVar(value="bl")

    mode_frame = tk.Frame(window, bg=THEME["bg_deep"])
    mode_frame.pack(fill=tk.X, padx=PAD_X, pady=(6, 4))

    mode_toggle = ctk.CTkSegmentedButton(
        mode_frame, values=["Home Server", "Broken Lands"],
        font=ctk.CTkFont(family=_FONT_FAMILY, size=12, weight="bold"),
        selected_color=THEME["mode_active"],
        selected_hover_color=THEME["bg_hover"],
        unselected_color=THEME["mode_inactive"],
        unselected_hover_color=THEME["bg_hover"],
        text_color=THEME["text_primary"],
        corner_radius=8, height=36,
        command=lambda val: switch_mode("rw" if val == "Home Server" else "bl"))
    mode_toggle.set("Broken Lands")
    mode_toggle.pack(fill=tk.X)

    # ============================================================
    # AUTO MODES — Two layouts swapped by mode toggle
    # ============================================================

    auto_frame = tk.Frame(window, bg=THEME["bg_deep"])
    auto_frame.pack(fill=tk.X, padx=PAD_X, pady=(4, 0))

    # -- State variables for all toggles --
    auto_quest_var = tk.BooleanVar(value=False)
    auto_titan_var = tk.BooleanVar(value=False)
    auto_groot_var = tk.BooleanVar(value=False)
    auto_pass_var = tk.BooleanVar(value=False)
    auto_occupy_var = tk.BooleanVar(value=False)
    auto_reinforce_var = tk.BooleanVar(value=False)
    auto_mithril_var = tk.BooleanVar(value=False)
    auto_gold_var = tk.BooleanVar(value=False)

    titan_interval_var = tk.StringVar(value=str(settings["titan_interval"]))
    groot_interval_var = tk.StringVar(value=str(settings["groot_interval"]))
    reinforce_interval_var = tk.StringVar(value=str(settings["reinforce_interval"]))
    pass_mode_var = tk.StringVar(value=settings["pass_mode"])
    pass_interval_var = tk.StringVar(value=str(settings["pass_interval"]))
    mithril_interval_var = tk.StringVar(value=str(settings.get("mithril_interval", 19)))
    config.MITHRIL_INTERVAL = settings.get("mithril_interval", 19)

    # -- helpers to turn each off (simplified — CTkSwitch auto-updates visually) --
    def _stop_quest():
        if auto_quest_var.get():
            auto_quest_var.set(False)
            stop_all_tasks_matching("_auto_quest")
            log.info("Stopping Auto Quest on all devices")

    def _stop_titan():
        if auto_titan_var.get():
            auto_titan_var.set(False)
            stop_all_tasks_matching("_auto_titan")
            log.info("Stopping Rally Titans on all devices")

    def _stop_groot():
        if auto_groot_var.get():
            auto_groot_var.set(False)
            stop_all_tasks_matching("_auto_groot")
            log.info("Stopping Join Groot on all devices")

    def _stop_pass_battle():
        if auto_pass_var.get():
            auto_pass_var.set(False)
            stop_all_tasks_matching("_auto_pass")
            log.info("Stopping Pass Battle on all devices")

    def _stop_occupy():
        if auto_occupy_var.get():
            auto_occupy_var.set(False)
            config.auto_occupy_running = False
            stop_all_tasks_matching("_auto_occupy")
            log.info("Stopping Occupy Towers on all devices")

    def _stop_reinforce_throne():
        if auto_reinforce_var.get():
            auto_reinforce_var.set(False)
            stop_all_tasks_matching("_auto_reinforce")
            log.info("Stopping Reinforce Throne on all devices")

    def _stop_mithril():
        config.MITHRIL_ENABLED = False
        config.MITHRIL_DEPLOY_TIME.clear()
        if auto_mithril_var.get():
            auto_mithril_var.set(False)
            stop_all_tasks_matching("_auto_mithril")
            log.info("Stopping Mine Mithril on all devices")

    def _stop_gold():
        if auto_gold_var.get():
            auto_gold_var.set(False)
            stop_all_tasks_matching("_auto_gold")
            log.info("Stopping Mine Gold on all devices")

    # ── Collapsible section helper ──
    def _make_section(parent, title, expanded=True):
        """Create a collapsible section with clickable header. Returns (container, inner)."""
        container = tk.Frame(parent, bg=THEME["bg_deep"])
        inner = tk.Frame(container, bg=THEME["bg_deep"])
        vis = tk.BooleanVar(value=expanded)
        arrow = "\u25BC" if expanded else "\u25B6"
        btn = ctk.CTkButton(container, text=f"  {arrow}  {title.upper()}",
                            font=ctk.CTkFont(family=_FONT_FAMILY, size=10, weight="bold"),
                            fg_color=THEME["bg_section"], hover_color=THEME["bg_hover"],
                            text_color=THEME["accent_cyan"], anchor="w",
                            corner_radius=6, height=28)
        def toggle_section():
            if vis.get():
                inner.pack_forget()
                vis.set(False)
                btn.configure(text=f"  \u25B6  {title.upper()}")
            else:
                inner.pack(fill=tk.X)
                vis.set(True)
                btn.configure(text=f"  \u25BC  {title.upper()}")
            _resize_window()
        btn.configure(command=toggle_section)
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

    # ── Row frames for side-by-side switch layout ──
    bl_combat_row1 = tk.Frame(auto_frame, bg=THEME["bg_deep"])
    bl_farming_row1 = tk.Frame(auto_frame, bg=THEME["bg_deep"])
    bl_farming_row2 = tk.Frame(auto_frame, bg=THEME["bg_deep"])
    rw_farming_row1 = tk.Frame(auto_frame, bg=THEME["bg_deep"])

    # ── Toggle switches (CTkSwitch — toggles variable BEFORE command fires) ──

    def toggle_auto_pass():
        if auto_pass_var.get():  # User just turned ON
            _stop_quest()
            _stop_occupy()
            for device in get_active_devices():
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

    pass_switch = ctk.CTkSwitch(auto_frame, text="Pass Battle", variable=auto_pass_var,
                                command=toggle_auto_pass, **_sw_kw)

    def toggle_auto_occupy():
        if auto_occupy_var.get():
            _stop_quest()
            _stop_pass_battle()
            for device in get_active_devices():
                task_key = f"{device}_auto_occupy"
                if task_key not in running_tasks:
                    stop_event = threading.Event()
                    launch_task(device, "auto_occupy",
                                run_auto_occupy, stop_event, args=(device, stop_event))
                    log.info("Started Auto Occupy on %s", device)
        else:
            _stop_occupy()

    occupy_switch = ctk.CTkSwitch(auto_frame, text="Occupy Towers", variable=auto_occupy_var,
                                  command=toggle_auto_occupy, **_sw_kw)

    def toggle_auto_reinforce():
        if auto_reinforce_var.get():
            for device in get_active_devices():
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

    reinforce_switch = ctk.CTkSwitch(auto_frame, text="Reinforce Throne", variable=auto_reinforce_var,
                                     command=toggle_auto_reinforce, **_sw_kw)

    def toggle_auto_quest():
        if auto_quest_var.get():
            _stop_pass_battle()
            _stop_occupy()
            _stop_gold()
            for device in get_active_devices():
                task_key = f"{device}_auto_quest"
                if task_key not in running_tasks:
                    stop_event = threading.Event()
                    launch_task(device, "auto_quest",
                                run_auto_quest, stop_event, args=(device, stop_event))
                    log.info("Started Auto Quest on %s", device)
        else:
            _stop_quest()

    quest_switch = ctk.CTkSwitch(auto_frame, text="Auto Quest", variable=auto_quest_var,
                                 command=toggle_auto_quest, **_sw_kw)

    def toggle_auto_titan():
        if auto_titan_var.get():
            _stop_groot()
            _stop_gold()
            for device in get_active_devices():
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

    titan_switch = ctk.CTkSwitch(auto_frame, text="Rally Titans", variable=auto_titan_var,
                                 command=toggle_auto_titan, **_sw_kw)

    def toggle_auto_groot():
        if auto_groot_var.get():
            _stop_titan()
            for device in get_active_devices():
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

    groot_switch = ctk.CTkSwitch(auto_frame, text="Join Groot", variable=auto_groot_var,
                                 command=toggle_auto_groot, **_sw_kw)

    def toggle_auto_mithril():
        if auto_mithril_var.get():
            config.MITHRIL_ENABLED = True
            config.MITHRIL_INTERVAL = int(mithril_interval_var.get())
            for device in get_active_devices():
                task_key = f"{device}_auto_mithril"
                if task_key not in running_tasks:
                    stop_event = threading.Event()
                    launch_task(device, "auto_mithril",
                                run_auto_mithril, stop_event, args=(device, stop_event))
                    log.info("Started Mine Mithril on %s", device)
        else:
            _stop_mithril()

    mithril_switch = ctk.CTkSwitch(auto_frame, text="Mine Mithril", variable=auto_mithril_var,
                                   command=toggle_auto_mithril, **_sw_kw)

    def toggle_auto_gold():
        if auto_gold_var.get():
            _stop_quest()
            _stop_titan()
            for device in get_active_devices():
                task_key = f"{device}_auto_gold"
                if task_key not in running_tasks:
                    stop_event = threading.Event()
                    launch_task(device, "auto_gold",
                                run_auto_gold, stop_event, args=(device, stop_event))
                    log.info("Started Mine Gold on %s", device)
        else:
            _stop_gold()

    gold_switch = ctk.CTkSwitch(auto_frame, text="Mine Gold", variable=auto_gold_var,
                                command=toggle_auto_gold, **_sw_kw)

    # ── Layout helpers — pack switches into the right sections ──

    def _layout_bl():
        """Pack BL mode: Combat (Pass+Occupy, Reinforce) then Farming (Quest+Titans, Gold+Mithril)."""
        bl_combat_ctr.pack(fill=tk.X, in_=auto_frame)
        bl_combat_row1.pack(fill=tk.X, padx=4, pady=2, in_=bl_combat_inner)
        pass_switch.pack(side=tk.LEFT, expand=True, fill=tk.BOTH, padx=4, in_=bl_combat_row1)
        occupy_switch.pack(side=tk.LEFT, expand=True, fill=tk.BOTH, padx=4, in_=bl_combat_row1)
        reinforce_switch.pack(fill=tk.X, padx=8, pady=2, in_=bl_combat_inner)

        bl_farming_ctr.pack(fill=tk.X, pady=(4, 0), in_=auto_frame)
        bl_farming_row1.pack(fill=tk.X, padx=4, pady=2, in_=bl_farming_inner)
        quest_switch.pack(side=tk.LEFT, expand=True, fill=tk.BOTH, padx=4, in_=bl_farming_row1)
        titan_switch.pack(side=tk.LEFT, expand=True, fill=tk.BOTH, padx=4, in_=bl_farming_row1)
        bl_farming_row2.pack(fill=tk.X, padx=4, pady=2, in_=bl_farming_inner)
        gold_switch.pack(side=tk.LEFT, expand=True, fill=tk.BOTH, padx=4, in_=bl_farming_row2)
        mithril_switch.pack(side=tk.LEFT, expand=True, fill=tk.BOTH, padx=4, in_=bl_farming_row2)

    def _layout_rw():
        """Pack Home Server mode: Events (Groot), Farming (Titans+Gold, Mithril), Combat (Reinforce)."""
        rw_events_ctr.pack(fill=tk.X, in_=auto_frame)
        groot_switch.pack(fill=tk.X, padx=8, pady=2, in_=rw_events_inner)

        rw_farming_ctr.pack(fill=tk.X, pady=(4, 0), in_=auto_frame)
        rw_farming_row1.pack(fill=tk.X, padx=4, pady=2, in_=rw_farming_inner)
        titan_switch.pack(side=tk.LEFT, expand=True, fill=tk.BOTH, padx=4, in_=rw_farming_row1)
        gold_switch.pack(side=tk.LEFT, expand=True, fill=tk.BOTH, padx=4, in_=rw_farming_row1)
        mithril_switch.pack(fill=tk.X, padx=8, pady=2, in_=rw_farming_inner)

        rw_combat_ctr.pack(fill=tk.X, pady=(4, 0), in_=auto_frame)
        reinforce_switch.pack(fill=tk.X, padx=8, pady=2, in_=rw_combat_inner)

    def _forget_all_toggles():
        """Forget all toggle switches, row frames, and section containers from layout."""
        for w in [pass_switch, occupy_switch, reinforce_switch, quest_switch,
                  titan_switch, groot_switch, mithril_switch, gold_switch,
                  bl_combat_row1, bl_farming_row1, bl_farming_row2, rw_farming_row1,
                  bl_combat_ctr, bl_farming_ctr,
                  rw_events_ctr, rw_farming_ctr, rw_combat_ctr]:
            w.pack_forget()

    # ── Mode switching ──

    def switch_mode(new_mode):
        if mode_var.get() == new_mode:
            return

        mode_var.set(new_mode)
        _forget_all_toggles()

        if new_mode == "rw":
            _layout_rw()
            bl_settings_row.pack_forget()
            mode_toggle.set("Home Server")
        else:
            _layout_bl()
            bl_settings_row.pack(fill=tk.X, padx=8, pady=(2, 0), in_=intervals_group, before=rw_settings_row)
            mode_toggle.set("Broken Lands")

        _resize_window()

    # Pack initial layout (BL default)
    _layout_bl()

    # ============================================================
    # SETTINGS (grouped, mode-aware)
    # ============================================================

    settings_container = tk.Frame(window, bg=THEME["bg_deep"])
    settings_container.pack(fill=tk.X, padx=PAD_X, pady=(6, 4))

    settings_visible = tk.BooleanVar(value=False)
    settings_card = ctk.CTkFrame(settings_container, fg_color=THEME["bg_card"], corner_radius=14,
                                 border_width=1, border_color=THEME["border_subtle"])

    def toggle_settings():
        if settings_visible.get():
            settings_card.pack_forget()
            settings_visible.set(False)
            settings_btn.configure(text="\u2699  Settings  \u25B6")
        else:
            settings_card.pack(fill=tk.X, pady=(0, 4))
            settings_visible.set(True)
            settings_btn.configure(text="\u2699  Settings  \u25BC")
        _resize_window()

    settings_btn = ctk.CTkButton(settings_container, text="\u2699  Settings  \u25B6",
                                 command=toggle_settings,
                                 font=ctk.CTkFont(family=_FONT_FAMILY, size=11, weight="bold"),
                                 fg_color=THEME["bg_section"], hover_color=THEME["bg_hover"],
                                 text_color=THEME["accent_cyan"], corner_radius=8, height=30)
    settings_btn.pack(fill=tk.X)

    def _make_group(parent, title):
        """Create a settings group frame with a header label."""
        frame = ctk.CTkFrame(parent, fg_color=THEME["bg_section"], corner_radius=8)
        ctk.CTkLabel(frame, text=title,
                     font=ctk.CTkFont(family=_FONT_FAMILY, size=9, weight="bold"),
                     text_color=THEME["text_muted"]).pack(anchor=tk.W, padx=8, pady=(4, 0))
        return frame

    # ── General ──
    general_group = _make_group(settings_card, "General")
    general_group.pack(fill=tk.X, padx=10, pady=(2, 4))

    general_row = ctk.CTkFrame(general_group, fg_color="transparent")
    general_row.pack(fill=tk.X, padx=8, pady=(0, 4))

    auto_heal_var = tk.BooleanVar(value=settings["auto_heal"])
    set_auto_heal(settings["auto_heal"])

    def toggle_auto_heal():
        set_auto_heal(auto_heal_var.get())
        save_current_settings()

    ctk.CTkCheckBox(general_row, text="Auto Heal", variable=auto_heal_var,
                    command=toggle_auto_heal, **_cb_kw).pack(side=tk.LEFT)

    verbose_var = tk.BooleanVar(value=settings.get("verbose_logging", False))
    from botlog import set_console_verbose
    set_console_verbose(verbose_var.get())

    def toggle_verbose():
        set_console_verbose(verbose_var.get())
        save_current_settings()

    web_dash_var = tk.BooleanVar(value=settings.get("web_dashboard", False))

    def toggle_web_dashboard():
        if web_dash_var.get():
            import importlib.util
            if importlib.util.find_spec("flask") is None:
                if messagebox.askyesno(
                    "Install Required",
                    "The web dashboard requires Flask (~10 MB download).\n\n"
                    "Install it now?"):
                    def _install():
                        try:
                            subprocess.run([sys.executable, "-m", "pip", "install", "flask"],
                                           capture_output=True, timeout=120)
                            messagebox.showinfo("Installed",
                                "Flask installed successfully!\n\n"
                                "Restart PACbot to start the dashboard.")
                        except Exception as ex:
                            messagebox.showerror("Error", f"Failed to install Flask:\n{ex}")
                    threading.Thread(target=_install, daemon=True).start()
                else:
                    web_dash_var.set(False)
                    return
            else:
                import socket as _sock
                try:
                    _s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
                    _s.connect(("8.8.8.8", 80))
                    _ip = _s.getsockname()[0]
                    _s.close()
                except Exception:
                    _ip = "your-pc-ip"
                messagebox.showinfo("Web Dashboard",
                    f"Web dashboard will start on next restart.\n\n"
                    f"On your phone, open Safari and go to:\n"
                    f"http://{_ip}:8080")
        save_current_settings()

    ctk.CTkCheckBox(general_row, text="Web", variable=web_dash_var,
                    command=toggle_web_dashboard, **_cb_kw).pack(side=tk.RIGHT)
    ctk.CTkCheckBox(general_row, text="Verbose Log", variable=verbose_var,
                    command=toggle_verbose, **_cb_kw).pack(side=tk.RIGHT, padx=(0, 8))

    # ── Auto Quest ──
    aq_group = _make_group(settings_card, "Auto Quest")
    aq_group.pack(fill=tk.X, padx=10, pady=(0, 4))

    aq_row1 = ctk.CTkFrame(aq_group, fg_color="transparent")
    aq_row1.pack(fill=tk.X, padx=8)

    eg_rally_own_var = tk.BooleanVar(value=settings.get("eg_rally_own", True))
    set_eg_rally_own(settings.get("eg_rally_own", True))

    def toggle_eg_rally_own():
        set_eg_rally_own(eg_rally_own_var.get())
        save_current_settings()

    ctk.CTkCheckBox(aq_row1, text="Rally Own EG", variable=eg_rally_own_var,
                    command=toggle_eg_rally_own, **_cb_kw).pack(side=tk.LEFT)

    titan_rally_own_var = tk.BooleanVar(value=settings.get("titan_rally_own", True))
    set_titan_rally_own(settings.get("titan_rally_own", True))

    def toggle_titan_rally_own():
        set_titan_rally_own(titan_rally_own_var.get())
        save_current_settings()

    ctk.CTkCheckBox(aq_row1, text="Rally Own Titans", variable=titan_rally_own_var,
                    command=toggle_titan_rally_own, **_cb_kw).pack(side=tk.LEFT, padx=(6, 0))

    aq_row2 = ctk.CTkFrame(aq_group, fg_color="transparent")
    aq_row2.pack(fill=tk.X, padx=8, pady=(2, 0))

    gather_enabled_var = tk.BooleanVar(value=settings.get("gather_enabled", True))
    gather_mine_level_var = tk.StringVar(value=str(settings.get("gather_mine_level", 4)))
    gather_max_troops_var = tk.StringVar(value=str(settings.get("gather_max_troops", 3)))
    set_gather_options(settings.get("gather_enabled", True),
                       settings.get("gather_mine_level", 4),
                       settings.get("gather_max_troops", 3))

    def update_gather_options(*_args):
        try:
            level = int(gather_mine_level_var.get())
            max_t = int(gather_max_troops_var.get())
            set_gather_options(gather_enabled_var.get(), level, max_t)
            save_current_settings()
        except ValueError:
            pass

    ctk.CTkCheckBox(aq_row2, text="Gather Gold", variable=gather_enabled_var,
                    command=update_gather_options, **_cb_kw).pack(side=tk.LEFT)
    ctk.CTkLabel(aq_row2, text="Mine Lv",
                 font=ctk.CTkFont(family=_FONT_FAMILY, size=10),
                 text_color=THEME["text_muted"]).pack(side=tk.LEFT, padx=(8, 0))
    ctk.CTkOptionMenu(aq_row2, variable=gather_mine_level_var,
                      values=["4", "5", "6"], command=update_gather_options,
                      **_om_kw, width=50).pack(side=tk.LEFT, padx=(2, 4))
    ctk.CTkLabel(aq_row2, text="Max Troops",
                 font=ctk.CTkFont(family=_FONT_FAMILY, size=10),
                 text_color=THEME["text_muted"]).pack(side=tk.LEFT, padx=(4, 0))
    ctk.CTkOptionMenu(aq_row2, variable=gather_max_troops_var,
                      values=["1", "2", "3", "4", "5"], command=update_gather_options,
                      **_om_kw, width=50).pack(side=tk.LEFT, padx=(2, 4))

    # Tower quest toggle
    aq_row3 = ctk.CTkFrame(aq_group, fg_color="transparent")
    aq_row3.pack(fill=tk.X, padx=8, pady=(2, 4))

    tower_quest_var = tk.BooleanVar(value=settings.get("tower_quest_enabled", False))
    set_tower_quest_enabled(settings.get("tower_quest_enabled", False))

    def update_tower_quest():
        enabled = tower_quest_var.get()
        if enabled:
            messagebox.showinfo("Tower Quest Setup",
                                "Mark your hive tower with the target marker in-game, then click OK.")
        set_tower_quest_enabled(enabled)
        save_current_settings()

    ctk.CTkCheckBox(aq_row3, text="Tower Quest", variable=tower_quest_var,
                    command=update_tower_quest, **_cb_kw).pack(side=tk.LEFT)
    ctk.CTkLabel(aq_row3, text="(mark tower with target marker first)",
                 font=ctk.CTkFont(family=_FONT_FAMILY, size=10),
                 text_color=THEME["text_muted"]).pack(side=tk.LEFT, padx=(4, 0))

    # ── AP Restoration ──
    ap_group = _make_group(settings_card, "AP Restoration")
    ap_group.pack(fill=tk.X, padx=10, pady=(0, 4))

    ap_toggle_row = ctk.CTkFrame(ap_group, fg_color="transparent")
    ap_toggle_row.pack(fill=tk.X, padx=8)

    auto_restore_ap_var = tk.BooleanVar(value=settings["auto_restore_ap"])
    set_auto_restore_ap(settings["auto_restore_ap"])

    ap_settings_row = ctk.CTkFrame(ap_group, fg_color="transparent")

    ap_use_free_var = tk.BooleanVar(value=settings["ap_use_free"])
    ap_use_potions_var = tk.BooleanVar(value=settings["ap_use_potions"])
    ap_allow_large_var = tk.BooleanVar(value=settings["ap_allow_large_potions"])
    ap_use_gems_var = tk.BooleanVar(value=settings["ap_use_gems"])
    ap_gem_limit_var = tk.StringVar(value=str(settings["ap_gem_limit"]))

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

    _ap_cb = dict(_cb_kw)
    _ap_cb["font"] = ctk.CTkFont(family=_FONT_FAMILY, size=11)
    ctk.CTkCheckBox(ap_settings_row, text="Free", variable=ap_use_free_var,
                    command=update_ap_options, **_ap_cb).pack(side=tk.LEFT)
    ctk.CTkCheckBox(ap_settings_row, text="Potions", variable=ap_use_potions_var,
                    command=update_ap_options, **_ap_cb).pack(side=tk.LEFT)
    ctk.CTkCheckBox(ap_settings_row, text="Large", variable=ap_allow_large_var,
                    command=update_ap_options, **_ap_cb).pack(side=tk.LEFT)
    ctk.CTkCheckBox(ap_settings_row, text="Gems", variable=ap_use_gems_var,
                    command=update_ap_options, **_ap_cb).pack(side=tk.LEFT)
    ctk.CTkLabel(ap_settings_row, text="Limit:",
                 font=ctk.CTkFont(family=_FONT_FAMILY, size=10),
                 text_color=THEME["text_muted"]).pack(side=tk.LEFT, padx=(4, 0))
    ctk.CTkEntry(ap_settings_row, textvariable=ap_gem_limit_var, width=50, **_entry_kw).pack(side=tk.LEFT, padx=(2, 0))
    ctk.CTkButton(ap_settings_row, text="Set", command=update_ap_options,
                  font=ctk.CTkFont(family=_FONT_FAMILY, size=10),
                  fg_color=THEME["btn_default"], hover_color=THEME["bg_hover"],
                  corner_radius=6, height=24, width=40).pack(side=tk.LEFT, padx=(2, 0))

    def toggle_auto_restore_ap():
        enabled = auto_restore_ap_var.get()
        set_auto_restore_ap(enabled)
        if enabled:
            ap_settings_row.pack(fill=tk.X, padx=8, pady=(2, 4))
        else:
            ap_settings_row.pack_forget()
        _resize_window()
        save_current_settings()

    ctk.CTkCheckBox(ap_toggle_row, text="Auto Restore AP", variable=auto_restore_ap_var,
                    command=toggle_auto_restore_ap, **_cb_kw).pack(side=tk.LEFT)

    if settings["auto_restore_ap"]:
        ap_settings_row.pack(fill=tk.X, padx=8, pady=(2, 4))

    # ── Troops ──
    troops_group = _make_group(settings_card, "Troops")
    troops_group.pack(fill=tk.X, padx=10, pady=(0, 4))

    troops_row = ctk.CTkFrame(troops_group, fg_color="transparent")
    troops_row.pack(fill=tk.X, padx=8, pady=(0, 4))

    ctk.CTkLabel(troops_row, text="Min Troops:",
                 font=ctk.CTkFont(family=_FONT_FAMILY, size=12),
                 text_color=THEME["text_primary"]).pack(side=tk.LEFT)
    min_troops_var = tk.StringVar(value=str(settings["min_troops"]))
    set_min_troops(settings["min_troops"])
    ctk.CTkEntry(troops_row, textvariable=min_troops_var, width=50, justify="center",
                 **_entry_kw).pack(side=tk.LEFT, padx=(4, 4))

    def update_min_troops():
        try:
            set_min_troops(int(min_troops_var.get()))
            save_current_settings()
        except:
            pass

    ctk.CTkButton(troops_row, text="Set", command=update_min_troops,
                  font=ctk.CTkFont(family=_FONT_FAMILY, size=10),
                  fg_color=THEME["btn_default"], hover_color=THEME["bg_hover"],
                  corner_radius=6, height=24, width=40).pack(side=tk.LEFT)

    variation_var = tk.StringVar(value=str(settings["variation"]))
    ctk.CTkLabel(troops_row, text="Randomize \u00b1",
                 font=ctk.CTkFont(family=_FONT_FAMILY, size=12),
                 text_color=THEME["text_primary"]).pack(side=tk.LEFT, padx=(16, 0))
    ctk.CTkEntry(troops_row, textvariable=variation_var, width=50, justify="center",
                 **_entry_kw).pack(side=tk.LEFT, padx=(4, 1))
    ctk.CTkLabel(troops_row, text="s",
                 font=ctk.CTkFont(family=_FONT_FAMILY, size=12),
                 text_color=THEME["text_muted"]).pack(side=tk.LEFT)

    # ── Intervals ──
    intervals_group = _make_group(settings_card, "Intervals")
    intervals_group.pack(fill=tk.X, padx=10, pady=(0, 4))

    # Pass mode & interval (BL only)
    bl_settings_row = ctk.CTkFrame(intervals_group, fg_color="transparent")

    ctk.CTkLabel(bl_settings_row, text="Pass",
                 font=ctk.CTkFont(family=_FONT_FAMILY, size=12),
                 text_color=THEME["text_muted"]).pack(side=tk.LEFT)
    ctk.CTkOptionMenu(bl_settings_row, variable=pass_mode_var,
                      values=["Rally Joiner", "Rally Starter"],
                      **_om_kw, width=120).pack(side=tk.LEFT, padx=(4, 4))
    ctk.CTkEntry(bl_settings_row, textvariable=pass_interval_var, width=50, justify="center",
                 **_entry_kw).pack(side=tk.LEFT, padx=(0, 1))
    ctk.CTkLabel(bl_settings_row, text="s",
                 font=ctk.CTkFont(family=_FONT_FAMILY, size=12),
                 text_color=THEME["text_muted"]).pack(side=tk.LEFT)

    # Titan / Groot / Reinforce / Mithril intervals (always visible)
    rw_settings_row = ctk.CTkFrame(intervals_group, fg_color="transparent")

    for lbl_text, var, unit in [("Titan", titan_interval_var, "s"),
                                 ("Groot", groot_interval_var, "s"),
                                 ("Reinf", reinforce_interval_var, "s"),
                                 ("Mithril", mithril_interval_var, "m")]:
        ctk.CTkLabel(rw_settings_row, text=lbl_text,
                     font=ctk.CTkFont(family=_FONT_FAMILY, size=12),
                     text_color=THEME["text_muted"]).pack(side=tk.LEFT, padx=(6, 0))
        ctk.CTkEntry(rw_settings_row, textvariable=var, width=50, justify="center",
                     **_entry_kw).pack(side=tk.LEFT, padx=(4, 1))
        ctk.CTkLabel(rw_settings_row, text=unit,
                     font=ctk.CTkFont(family=_FONT_FAMILY, size=12),
                     text_color=THEME["text_muted"]).pack(side=tk.LEFT)

    bl_settings_row.pack(fill=tk.X, padx=8, pady=(2, 0))
    rw_settings_row.pack(fill=tk.X, padx=8, pady=(2, 4))

    # Apply saved mode (hides BL-only widgets if Home Server)
    if settings["mode"] == "rw":
        switch_mode("rw")

    # ── Territory (collapsible) ──
    territory_container = tk.Frame(window, bg=THEME["bg_deep"])
    territory_container.pack(fill=tk.X, padx=PAD_X, pady=(0, 2))

    territory_visible = tk.BooleanVar(value=False)
    territory_inner = ctk.CTkFrame(territory_container, fg_color=THEME["bg_section"],
                                   corner_radius=8)

    def toggle_territory():
        if territory_visible.get():
            territory_inner.pack_forget()
            territory_visible.set(False)
            territory_btn.configure(text="Territory Settings  \u25B6")
        else:
            territory_inner.pack(fill=tk.X, padx=4, pady=(0, 4))
            territory_visible.set(True)
            territory_btn.configure(text="Territory Settings  \u25BC")
        _resize_window()

    territory_btn = ctk.CTkButton(territory_container, text="Territory Settings  \u25B6",
                                  command=toggle_territory,
                                  font=ctk.CTkFont(family=_FONT_FAMILY, size=11, weight="bold"),
                                  fg_color=THEME["bg_section"], hover_color=THEME["bg_hover"],
                                  text_color=THEME["accent_cyan"], corner_radius=8, height=30)
    territory_btn.pack(fill=tk.X)

    teams_row = ctk.CTkFrame(territory_inner, fg_color="transparent")
    teams_row.pack(fill=tk.X, padx=8, pady=(6, 2))
    ctk.CTkLabel(teams_row, text="My Team:",
                 font=ctk.CTkFont(family=_FONT_FAMILY, size=12),
                 text_color=THEME["text_primary"]).pack(side=tk.LEFT)
    my_team_var = tk.StringVar(value=settings["my_team"])
    ctk.CTkOptionMenu(teams_row, variable=my_team_var,
                      values=["yellow", "red", "blue", "green"],
                      command=lambda _: update_territory_config(),
                      width=80, **_om_kw).pack(side=tk.LEFT, padx=(4, 6))

    set_territory_config(settings["my_team"])

    def update_territory_config():
        set_territory_config(my_team_var.get())
        save_current_settings()

    def open_territory_mgr():
        active = get_active_devices()
        if active:
            threading.Thread(target=open_territory_manager, args=(active[0],), daemon=True).start()

    ctk.CTkButton(territory_inner, text="Territory Square Manager",
                  command=open_territory_mgr,
                  font=ctk.CTkFont(family=_FONT_FAMILY, size=11),
                  fg_color=THEME["btn_default"], hover_color=THEME["bg_hover"],
                  corner_radius=8, height=30).pack(fill=tk.X, padx=8, pady=(4, 8))

    # ============================================================
    # SETTINGS PERSISTENCE
    # ============================================================

    def save_current_settings():
        # Pull any web-dashboard changes into GUI vars before saving
        _pull_settings_from_file()
        existing = load_settings()

        # Build per-device troops dict
        dt = {}
        for dev_id, var in device_troops_vars.items():
            try:
                dt[dev_id] = int(var.get())
            except ValueError:
                dt[dev_id] = 5

        # Merge GUI values on top of existing settings
        existing.update({
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
            "mode": mode_var.get(),
            "verbose_logging": verbose_var.get(),
            "eg_rally_own": eg_rally_own_var.get(),
            "titan_rally_own": titan_rally_own_var.get(),
            "mithril_interval": int(mithril_interval_var.get()) if mithril_interval_var.get().isdigit() else 19,
            "web_dashboard": web_dash_var.get(),
            "gather_enabled": gather_enabled_var.get(),
            "gather_mine_level": int(gather_mine_level_var.get()) if gather_mine_level_var.get().isdigit() else 4,
            "gather_max_troops": int(gather_max_troops_var.get()) if gather_max_troops_var.get().isdigit() else 3,
            "tower_quest_enabled": tower_quest_var.get(),
            "device_troops": dt,
        })
        save_settings(existing)

    # ============================================================
    # MORE ACTIONS (collapsed by default)
    # ============================================================

    actions_container = tk.Frame(window, bg=THEME["bg_deep"])
    actions_container.pack(fill=tk.X, padx=PAD_X, pady=(2, 0))

    actions_visible = tk.BooleanVar(value=False)
    actions_inner = ctk.CTkFrame(actions_container, fg_color=THEME["bg_section"],
                                 corner_radius=8)

    def toggle_actions():
        if actions_visible.get():
            actions_inner.pack_forget()
            actions_visible.set(False)
            actions_toggle_btn.configure(text="More Actions  \u25B6")
        else:
            actions_inner.pack(fill=tk.X, padx=4, pady=(0, 4))
            actions_visible.set(True)
            actions_toggle_btn.configure(text="More Actions  \u25BC")
        _resize_window()

    actions_toggle_btn = ctk.CTkButton(actions_container, text="More Actions  \u25B6",
                                       command=toggle_actions,
                                       font=ctk.CTkFont(family=_FONT_FAMILY, size=11, weight="bold"),
                                       fg_color=THEME["bg_section"], hover_color=THEME["bg_hover"],
                                       text_color=THEME["accent_cyan"], corner_radius=8, height=30)
    actions_toggle_btn.pack(fill=tk.X)

    # Tabs inside collapsible section
    tabs = ctk.CTkTabview(actions_inner, fg_color=THEME["bg_card"],
                          segmented_button_fg_color=THEME["mode_inactive"],
                          segmented_button_selected_color=THEME["mode_active"],
                          segmented_button_selected_hover_color=THEME["bg_hover"],
                          segmented_button_unselected_color=THEME["mode_inactive"],
                          segmented_button_unselected_hover_color=THEME["bg_hover"],
                          text_color=THEME["text_primary"], corner_radius=8)
    tabs.pack(fill=tk.X, pady=(4, 4), padx=4)

    farm_tab = tabs.add("  Farm  ")
    war_tab = tabs.add("  War  ")
    debug_tab = tabs.add("  Debug  ")

    # ── Task row helpers ──
    task_row_enabled_vars = []

    def add_task_row(parent, name, default_interval):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
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

        ctk.CTkCheckBox(frame, text="", variable=enabled, command=toggle,
                        width=20, height=20, fg_color=THEME["accent_cyan"],
                        hover_color=THEME["bg_hover"], border_color=THEME["border_subtle"],
                        checkmark_color=THEME["bg_deep"], corner_radius=4).pack(side=tk.LEFT)
        ctk.CTkEntry(frame, textvariable=interval, width=50, justify="center",
                     **_entry_kw).pack(side=tk.LEFT, padx=(2, 0))
        ctk.CTkLabel(frame, text="s",
                     font=ctk.CTkFont(family=_FONT_FAMILY, size=10),
                     text_color=THEME["text_muted"]).pack(side=tk.LEFT, padx=(1, 4))

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

        ctk.CTkButton(frame, text=name, command=do_run_once,
                      font=ctk.CTkFont(family=_FONT_FAMILY, size=11),
                      fg_color=THEME["btn_default"], hover_color=THEME["bg_hover"],
                      text_color=THEME["text_primary"], corner_radius=8,
                      height=28).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

    def add_debug_button(parent, name, function):
        def do_run_once():
            for device in get_active_devices():
                task_key = f"{device}_once:{name}"
                if task_key in running_tasks:
                    info = running_tasks[task_key]
                    if isinstance(info, dict) and info.get("thread") and info["thread"].is_alive():
                        continue
                stop_event = threading.Event()
                launch_task(device, f"once:{name}",
                            run_once, stop_event, args=(device, name, function))

        ctk.CTkButton(parent, text=name, command=do_run_once,
                      font=ctk.CTkFont(family=_FONT_FAMILY, size=11),
                      fg_color=THEME["btn_default"], hover_color=THEME["bg_hover"],
                      text_color=THEME["text_primary"], corner_radius=8,
                      height=28).pack(pady=2, fill=tk.X)

    # Farm tab
    add_task_row(farm_tab, "Rally Evil Guard", 30)
    add_task_row(farm_tab, "Join Titan Rally", 30)
    add_task_row(farm_tab, "Join Evil Guard Rally", 30)
    add_task_row(farm_tab, "Join Groot Rally", 30)
    add_task_row(farm_tab, "Heal All", 30)
    add_task_row(farm_tab, "Gather Gold", 60)

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
    add_debug_button(debug_tab, "Diagnose Grid", diagnose_grid)
    add_debug_button(debug_tab, "Teleport Benchmark", teleport_benchmark)
    add_debug_button(debug_tab, "Mine Mithril", mine_mithril)

    ctk.CTkFrame(debug_tab, height=1, fg_color=THEME["border_subtle"]).pack(fill=tk.X, pady=6)

    tap_row = ctk.CTkFrame(debug_tab, fg_color="transparent")
    tap_row.pack(fill=tk.X, pady=2)
    ctk.CTkLabel(tap_row, text="Tap X:",
                 font=ctk.CTkFont(family=_FONT_FAMILY, size=12),
                 text_color=THEME["text_primary"]).pack(side=tk.LEFT)
    x_var = tk.StringVar()
    ctk.CTkEntry(tap_row, textvariable=x_var, width=60, **_entry_kw).pack(side=tk.LEFT, padx=(2, 8))
    ctk.CTkLabel(tap_row, text="Y:",
                 font=ctk.CTkFont(family=_FONT_FAMILY, size=12),
                 text_color=THEME["text_primary"]).pack(side=tk.LEFT)
    y_var = tk.StringVar()
    ctk.CTkEntry(tap_row, textvariable=y_var, width=60, **_entry_kw).pack(side=tk.LEFT, padx=(2, 8))

    def test_tap():
        try:
            x, y = int(x_var.get()), int(y_var.get())
            for device in get_active_devices():
                adb_tap(device, x, y)
                get_logger("main", device).debug("Test tapped (%s, %s)", x, y)
        except ValueError:
            log.warning("Invalid coordinates!")

    ctk.CTkButton(tap_row, text="Tap", command=test_tap,
                  font=ctk.CTkFont(family=_FONT_FAMILY, size=10),
                  fg_color=THEME["btn_default"], hover_color=THEME["bg_hover"],
                  corner_radius=6, height=24, width=50).pack(side=tk.LEFT)

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
        _stop_gold()
        for var in task_row_enabled_vars:
            var.set(False)
        for key in list(running_tasks.keys()):
            stop_task(key)
        config.DEVICE_STATUS.clear()
        log.info("=== ALL TASKS STOPPED ===")

    ctk.CTkButton(window, text="STOP ALL",
                  font=ctk.CTkFont(family=_FONT_FAMILY, size=13, weight="bold"),
                  fg_color=THEME["btn_danger"], hover_color=THEME["btn_danger_hover"],
                  text_color=THEME["text_white"], corner_radius=10, height=44,
                  command=stop_all).pack(fill=tk.X, padx=PAD_X, pady=(6, 2))

    quit_row = tk.Frame(window, bg=THEME["bg_deep"])
    quit_row.pack(pady=(4, 8))

    def restart():
        save_current_settings()
        stop_all()
        # Disconnect ADB devices before restarting
        try:
            from devices import get_devices
            for d in get_devices():
                try:
                    subprocess.run([config.adb_path, "disconnect", d],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=2)
                except Exception:
                    pass
        except Exception:
            pass
        from updater import check_and_update
        check_and_update()
        window.destroy()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _get_ram_gb():
        """Get total system RAM in human-readable format. Cross-platform."""
        try:
            if platform.system() == "Windows":
                import ctypes
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [("dwLength", ctypes.c_ulong),
                                ("dwMemoryLoad", ctypes.c_ulong),
                                ("ullTotalPhys", ctypes.c_ulonglong),
                                ("ullAvailPhys", ctypes.c_ulonglong),
                                ("ullTotalPageFile", ctypes.c_ulonglong),
                                ("ullAvailPageFile", ctypes.c_ulonglong),
                                ("ullTotalVirtual", ctypes.c_ulonglong),
                                ("ullAvailVirtual", ctypes.c_ulonglong),
                                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
                mem = MEMORYSTATUSEX()
                mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
                return f"{mem.ullTotalPhys / (1024**3):.1f} GB"
            elif platform.system() == "Darwin":
                import subprocess
                result = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                        capture_output=True, text=True, timeout=5)
                return f"{int(result.stdout.strip()) / (1024**3):.1f} GB"
            else:
                # Linux — read /proc/meminfo
                with open("/proc/meminfo") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            kb = int(line.split()[1])
                            return f"{kb / (1024**2):.1f} GB"
        except Exception:
            pass
        return "unknown"

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

                # Collect machine specs
                cpu_cores = os.cpu_count() or "unknown"
                cpu_arch = platform.machine()
                ram_gb = _get_ram_gb()

                info_lines = [
                    f"PACbot Bug Report",
                    f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    f"",
                    f"=== System ===",
                    f"Version: {version}",
                    f"Python: {sys.version}",
                    f"OS: {platform.system()} {platform.release()} ({platform.version()})",
                    f"CPU: {cpu_arch}, {cpu_cores} cores",
                    f"RAM: {ram_gb}",
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

    _bottom_btn_kw = dict(font=ctk.CTkFont(family=_FONT_FAMILY, size=11),
                          fg_color=THEME["bg_section"], hover_color=THEME["bg_hover"],
                          text_color=THEME["text_primary"], corner_radius=8, height=30,
                          border_width=1, border_color=THEME["border_subtle"])
    ctk.CTkButton(quit_row, text="Restart", command=restart, **_bottom_btn_kw).pack(side=tk.LEFT, padx=(0, 8))
    ctk.CTkButton(quit_row, text="Bug Report", command=export_bug_report, **_bottom_btn_kw).pack(side=tk.LEFT, padx=(0, 8))
    ctk.CTkButton(quit_row, text="Quit", command=lambda: on_close(), **_bottom_btn_kw).pack(side=tk.LEFT)

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

        # Auto-reset toggle vars when their threads die (CTkSwitch auto-updates visually)
        if auto_quest_var.get() and not any(k.endswith("_auto_quest") for k in running_tasks):
            auto_quest_var.set(False)
        if auto_titan_var.get() and not any(k.endswith("_auto_titan") for k in running_tasks):
            auto_titan_var.set(False)
        if auto_groot_var.get() and not any(k.endswith("_auto_groot") for k in running_tasks):
            auto_groot_var.set(False)
        if auto_pass_var.get() and not any(k.endswith("_auto_pass") for k in running_tasks):
            auto_pass_var.set(False)
        if auto_occupy_var.get() and not any(k.endswith("_auto_occupy") for k in running_tasks):
            auto_occupy_var.set(False)
        if auto_reinforce_var.get() and not any(k.endswith("_auto_reinforce") for k in running_tasks):
            auto_reinforce_var.set(False)
        if auto_mithril_var.get() and not any(k.endswith("_auto_mithril") for k in running_tasks):
            auto_mithril_var.set(False)
            config.MITHRIL_ENABLED = False
            config.MITHRIL_DEPLOY_TIME.clear()
        if auto_gold_var.get() and not any(k.endswith("_auto_gold") for k in running_tasks):
            auto_gold_var.set(False)

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

    _pill_font = ctk.CTkFont(family=_FONT_FAMILY, size=10, weight="bold")
    _quest_font = ctk.CTkFont(family=_FONT_FAMILY, size=10)

    def update_device_cards():
        """Update per-device status text, troop pills, and quest pills."""
        for dev_id, refs in device_display.items():
            # ── Status text ──
            msg = config.DEVICE_STATUS.get(dev_id, "Idle")
            lbl = refs["status_label"]
            lbl.configure(text=msg)
            if msg == "Idle":
                lbl.configure(text_color=THEME["text_muted"])
            elif "Waiting" in msg:
                lbl.configure(text_color=THEME["accent_amber"])
            elif "Navigating" in msg:
                lbl.configure(text_color=THEME["text_muted"])
            else:
                lbl.configure(text_color=THEME["accent_cyan"])

            # ── Troop pills ──
            troop_frame = refs["troop_frame"]
            for child in troop_frame.winfo_children():
                child.destroy()

            snapshot = get_troop_status(dev_id)
            if snapshot and snapshot.troops:
                for t in snapshot.troops:
                    bg, fg = TROOP_PILL_COLORS.get(t.action, ("#1a2540", "#64b5f6"))
                    if t.is_home:
                        text = "Home"
                    elif t.time_left is not None and t.time_left > 0:
                        m, s = divmod(t.time_left, 60)
                        text = f"{t.action.value} {m}:{s:02d}"
                    else:
                        text = t.action.value
                    ctk.CTkLabel(troop_frame, text=text, font=_pill_font,
                                 fg_color=bg, text_color=fg,
                                 corner_radius=10, height=20,
                                 padx=8, pady=0).pack(side=tk.LEFT, padx=(0, 4))

                age = int(snapshot.age_seconds)
                if age > 5:
                    age_text = f"{age}s ago" if age < 60 else f"{age // 60}m ago"
                    ctk.CTkLabel(troop_frame, text=age_text,
                                 font=ctk.CTkFont(family=_FONT_FAMILY, size=9),
                                 text_color="#556677").pack(side=tk.RIGHT)

            # ── Quest pills ──
            quest_frame = refs["quest_frame"]
            for child in quest_frame.winfo_children():
                child.destroy()

            quests = get_quest_tracking_state(dev_id)
            if quests:
                for q in quests:
                    raw = q["quest_type"].replace("QuestType.", "")
                    label = QUEST_LABELS.get(q["quest_type"], raw)
                    seen = q.get("last_seen") or 0
                    tgt = q.get("target")
                    pend = q.get("pending", 0)
                    # Skip completed quests
                    if tgt is not None and seen >= tgt and pend == 0:
                        continue
                    val_text = f"{label} {seen}"
                    if tgt is not None:
                        val_text += f"/{tgt}"
                    if pend > 0:
                        val_text += f" +{pend}"
                    tk.Label(quest_frame, text=val_text, bg="#1e1e38",
                             fg="#8899bb", font=(_FONT_FAMILY, 9),
                             padx=8, pady=2).pack(side=tk.LEFT, padx=(0, 4), pady=(2, 0))

        window.after(2000, update_device_cards)

    window.after(2000, update_device_cards)

    _last_settings_mtime = [0.0]

    def _pull_settings_from_file():
        """Sync GUI vars from settings.json if it changed on disk.

        Called both by the periodic timer and by save_current_settings()
        (to avoid overwriting web-dashboard changes with stale GUI vars).
        """
        try:
            mtime = os.path.getmtime(SETTINGS_FILE)
            if mtime > _last_settings_mtime[0]:
                _last_settings_mtime[0] = mtime
                s = load_settings()
                auto_heal_var.set(s["auto_heal"])
                auto_restore_ap_var.set(s["auto_restore_ap"])
                ap_use_free_var.set(s["ap_use_free"])
                ap_use_potions_var.set(s["ap_use_potions"])
                ap_allow_large_var.set(s["ap_allow_large_potions"])
                ap_use_gems_var.set(s["ap_use_gems"])
                ap_gem_limit_var.set(str(s["ap_gem_limit"]))
                min_troops_var.set(str(s["min_troops"]))
                variation_var.set(str(s["variation"]))
                titan_interval_var.set(str(s["titan_interval"]))
                groot_interval_var.set(str(s["groot_interval"]))
                reinforce_interval_var.set(str(s["reinforce_interval"]))
                pass_interval_var.set(str(s["pass_interval"]))
                pass_mode_var.set(s["pass_mode"])
                my_team_var.set(s["my_team"])
                verbose_var.set(s.get("verbose_logging", False))
                eg_rally_own_var.set(s.get("eg_rally_own", True))
                titan_rally_own_var.set(s.get("titan_rally_own", True))
                mithril_interval_var.set(str(s.get("mithril_interval", 19)))
                web_dash_var.set(s.get("web_dashboard", False))
                gather_enabled_var.set(s.get("gather_enabled", True))
                gather_mine_level_var.set(str(s.get("gather_mine_level", 4)))
                gather_max_troops_var.set(str(s.get("gather_max_troops", 3)))
                mode_var.set(s.get("mode", "bl"))
        except Exception:
            pass

    def _sync_settings_timer():
        _pull_settings_from_file()
        window.after(5000, _sync_settings_timer)

    # Initialize mtime so we don't re-apply on first tick
    try:
        _last_settings_mtime[0] = os.path.getmtime(SETTINGS_FILE)
    except Exception:
        pass
    window.after(5000, _sync_settings_timer)

    def update_mithril_timer():
        """Update the mithril switch text with elapsed time since deploy."""
        if auto_mithril_var.get() and config.MITHRIL_DEPLOY_TIME:
            earliest = min(config.MITHRIL_DEPLOY_TIME.values())
            elapsed = int(time.time() - earliest)
            mm, ss = divmod(elapsed, 60)
            mithril_switch.configure(text=f"Mine Mithril ({mm:02d}:{ss:02d})")
        elif auto_mithril_var.get():
            mithril_switch.configure(text="Mine Mithril")
        window.after(1000, update_mithril_timer)

    window.after(1000, update_mithril_timer)

    def on_close():
        try:
            save_current_settings()
        except Exception as e:
            print(f"Failed to save settings: {e}")
        try:
            stop_all()
        except Exception:
            pass
        from startup import shutdown
        shutdown()
        try:
            window.destroy()
        except Exception:
            pass
        os._exit(0)

    window.protocol("WM_DELETE_WINDOW", on_close)

    # ============================================================
    # WEB DASHBOARD (opt-in, background thread)
    # ============================================================

    if settings.get("web_dashboard", False):
        try:
            from web.dashboard import create_app, get_local_ip, ensure_firewall_open
            import logging as _wlog
            # Suppress Flask's default request logging in console
            _wlog.getLogger("werkzeug").setLevel(_wlog.WARNING)

            # Open Windows Firewall for remote phone access
            ensure_firewall_open(8080)

            def _start_web_dashboard():
                from werkzeug.serving import make_server
                app = create_app()
                srv = make_server("0.0.0.0", 8080, app, threaded=True)
                srv.socket.setsockopt(
                    __import__("socket").SOL_SOCKET,
                    __import__("socket").SO_REUSEADDR, 1)
                srv.serve_forever()

            _web_thread = threading.Thread(target=_start_web_dashboard, daemon=True)
            _web_thread.start()
            _web_ip = get_local_ip()
            _web_url = f"http://{_web_ip}:8080"
            log.info("Web dashboard started at %s", _web_url)
            _web_open_url[0] = _web_url
        except ImportError:
            log.info("Web dashboard enabled but Flask not installed. "
                     "Install with: pip install flask")
        except Exception as e:
            log.warning("Failed to start web dashboard: %s", e)

    # ============================================================
    # RELAY TUNNEL (opt-in, connects to remote relay server)
    # ============================================================

    if settings.get("relay_enabled", False):
        _relay_url = settings.get("relay_url", "")
        _relay_secret = settings.get("relay_secret", "")
        _relay_bot = settings.get("relay_bot_name", "")
        if _relay_url and _relay_secret and _relay_bot:
            try:
                from tunnel import start_tunnel, tunnel_status
                start_tunnel(_relay_url, _relay_secret, _relay_bot)
                # Override Web App link to point to the relay
                _relay_host = _relay_url.replace("ws://", "").replace("wss://", "")
                _relay_host = _relay_host.split("/")[0]  # just host:port
                _web_open_url[0] = f"http://{_relay_host}/{_relay_bot}/"
                web_link_btn.configure(text="Remote Dashboard")
            except ImportError:
                log.info("Relay tunnel enabled but 'websockets' not installed. "
                         "Install with: pip install websockets")
            except Exception as e:
                log.warning("Failed to start relay tunnel: %s", e)

    _resize_window()

    window.mainloop()

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    from startup import initialize
    initialize()
    create_gui()
