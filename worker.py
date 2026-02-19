"""
PACbot Worker — runs a single task for a single device in its own console window.
Launched by main.py as a subprocess with CREATE_NEW_CONSOLE.
"""
import argparse
import os
import sys
import time
import threading
import traceback

# Set up config before importing anything else
import config
from config import set_min_troops, set_auto_heal, set_territory_config

def parse_args():
    parser = argparse.ArgumentParser(description="PACbot Worker")
    parser.add_argument("--device", required=True, help="ADB device ID")
    parser.add_argument("--task", required=True, help="Task to run")
    parser.add_argument("--stop-file", required=True, help="Path to stop flag file")
    parser.add_argument("--min-troops", type=int, default=0)
    parser.add_argument("--auto-heal", type=int, default=1)
    parser.add_argument("--my-team", default="yellow")
    parser.add_argument("--enemy-teams", default="green")
    parser.add_argument("--pass-mode", default="Rally Joiner")
    parser.add_argument("--pass-interval", type=int, default=30)
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--manual-attack-squares", default="")
    parser.add_argument("--manual-ignore-squares", default="")
    return parser.parse_args()

def deserialize_squares(s):
    if not s:
        return set()
    result = set()
    for pair in s.split(";"):
        parts = pair.split(",")
        if len(parts) == 2:
            result.add((int(parts[0]), int(parts[1])))
    return result

def is_stopped(stop_file):
    return os.path.exists(stop_file)

def make_stop_check(stop_file):
    return lambda: is_stopped(stop_file)

# ============================================================
# TASK IMPLEMENTATIONS
# ============================================================

def run_auto_quest(device, stop_check):
    from troops import troops_avail
    from actions import check_quests

    print(f"[{device}] Auto Quest started")
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

def run_auto_occupy(device, stop_check):
    from territory import auto_occupy_loop

    config.auto_occupy_running = True

    # Monitor stop file in background and set config flag when stopped
    def monitor():
        while not stop_check():
            time.sleep(1)
        config.auto_occupy_running = False

    threading.Thread(target=monitor, daemon=True).start()
    auto_occupy_loop(device)
    print(f"[{device}] Auto Occupy stopped")

def run_auto_pass(device, stop_check, pass_mode, pass_interval):
    from actions import target, join_war_rallies
    from troops import troops_avail, heal_all
    from vision import adb_tap, tap_image, load_screenshot, find_image, wait_for_image_and_tap

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
                # Write alert file so GUI can show messagebox
                alert_dir = os.path.dirname(args.stop_file)
                safe_dev = device.replace(":", "_")
                alert_path = os.path.join(alert_dir, f"alert_{safe_dev}_no_marker.flag")
                with open(alert_path, "w") as f:
                    f.write("no_marker")
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

def run_repeat(device, task_name, function, interval, stop_check):
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
# FUNCTION LOOKUP
# ============================================================

def get_task_function(name):
    from actions import (attack, target, check_quests, teleport,
                         rally_titan, rally_eg, join_rally, join_war_rallies)
    from territory import attack_territory, sample_specific_squares
    from troops import troops_avail, heal_all
    from navigation import check_screen
    from vision import load_screenshot

    FUNCTIONS = {
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
    return FUNCTIONS.get(name)

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    args = parse_args()

    device = args.device
    task = args.task
    stop_file = args.stop_file

    # Set console window title
    safe_title = f"PACbot - {device} - {task}"
    if sys.platform == "win32":
        os.system(f'title {safe_title}')

    print(f"=" * 50)
    print(f"  PACbot Worker")
    print(f"  Device: {device}")
    print(f"  Task:   {task}")
    print(f"=" * 50)
    print()

    # Apply config from args
    set_min_troops(args.min_troops)
    set_auto_heal(bool(args.auto_heal))
    set_territory_config(args.my_team, args.enemy_teams.split(","))
    config.MANUAL_ATTACK_SQUARES = deserialize_squares(args.manual_attack_squares)
    config.MANUAL_IGNORE_SQUARES = deserialize_squares(args.manual_ignore_squares)

    stop_check = make_stop_check(stop_file)

    # Dispatch
    if task == "auto_quest":
        run_auto_quest(device, stop_check)

    elif task == "auto_occupy":
        run_auto_occupy(device, stop_check)

    elif task == "auto_pass":
        run_auto_pass(device, stop_check, args.pass_mode, args.pass_interval)

    elif task.startswith("repeat:"):
        func_name = task[7:]  # strip "repeat:"
        func = get_task_function(func_name)
        if func:
            run_repeat(device, func_name, func, args.interval, stop_check)
        else:
            print(f"Unknown function: {func_name}")

    elif task.startswith("once:"):
        func_name = task[5:]  # strip "once:"
        func = get_task_function(func_name)
        if func:
            run_once(device, func_name, func)
        else:
            print(f"Unknown function: {func_name}")

    else:
        print(f"Unknown task: {task}")

    # Cleanup
    if os.path.exists(stop_file):
        try:
            os.remove(stop_file)
        except:
            pass

    print()
    print(f"[{device}] Worker finished. Press Enter to close...")
    try:
        input()
    except:
        pass
