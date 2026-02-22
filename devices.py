import subprocess
import platform

from config import adb_path

# ============================================================
# KNOWN EMULATOR ADB PORTS (for auto-connect)
# ============================================================

# MuMu Player 12: base port 16384, +32 per instance (up to 8 instances)
# MuMu Player 5:  base port 5555, +2 per instance
# BlueStacks 5:   base port 5555, +10 per instance (5555, 5565, 5575, 5585)
EMULATOR_PORTS = [5555 + (i * 10) for i in range(8)]      # BlueStacks
EMULATOR_PORTS += [5555 + (i * 2) for i in range(8)]       # MuMu 5
EMULATOR_PORTS += [16384 + (i * 32) for i in range(8)]     # MuMu 12
EMULATOR_PORTS = sorted(set(EMULATOR_PORTS))

# ============================================================
# DEVICE DETECTION (cross-platform, multi-emulator)
# ============================================================


def _auto_connect():
    """Silently try to adb-connect known emulator ports so they show up in 'adb devices'."""
    for port in EMULATOR_PORTS:
        addr = f"127.0.0.1:{port}"
        try:
            subprocess.run(
                [adb_path, "connect", addr],
                capture_output=True, text=True, timeout=3
            )
        except (subprocess.TimeoutExpired, Exception):
            pass


def get_devices():
    """Get list of all connected ADB devices, deduplicating emulator/IP pairs."""
    try:
        _auto_connect()
        result = subprocess.run([adb_path, "devices"], capture_output=True, text=True, timeout=10)
        lines = result.stdout.strip().split('\n')[1:]  # Skip "List of devices attached"
        devices = [line.split()[0] for line in lines if line.strip() and 'device' in line]

        # Deduplicate: if both "emulator-5554" and "127.0.0.1:5555" exist,
        # they are the same instance. Keep only the emulator-XXXX form.
        emulator_ports = set()
        for d in devices:
            if d.startswith("emulator-"):
                # ADB console port is the emulator-XXXX number,
                # the ADB data port is console_port + 1
                emulator_ports.add(int(d.split("-")[1]) + 1)

        deduped = []
        for d in devices:
            if ":" in d and not d.startswith("emulator-"):
                port = int(d.split(":")[1])
                if port in emulator_ports:
                    continue  # skip, already have emulator-XXXX for this
            deduped.append(d)

        return deduped
    except Exception as e:
        print(f"Failed to get devices: {e}")
        return []

def get_emulator_instances():
    """Get mapping of device IDs to friendly display names.

    On Windows: tries to map ADB devices to emulator window titles
                (supports BlueStacks and MuMu Player).
    On macOS/Linux: uses ADB device IDs as display names.
    """
    devices = get_devices()

    if platform.system() == "Windows":
        return _get_emulator_instances_windows(devices)

    # macOS / Linux — no window mapping, just use device IDs
    print(f"Found devices: {devices}")
    return {device: device for device in devices}

# ============================================================
# WINDOWS-ONLY: emulator window name mapping
# ============================================================

def _get_emulator_instances_windows(devices):
    """Map ADB devices to emulator window names using Win32 APIs.

    Supports BlueStacks (HD-Player) and MuMu Player (MuMuVMMHeadless / MuMuPlayer).
    """
    try:
        import win32gui
        import win32process
        import psutil
    except ImportError:
        print("pywin32/psutil not installed — using device IDs")
        return {d: d for d in devices}

    try:
        emulator_windows = {}

        # Process name patterns for supported emulators
        EMULATOR_PROCESS_NAMES = [
            "hd-player",       # BlueStacks
            "bluestacks",      # BlueStacks (alt)
            "mumuplayer",      # MuMu Player
            "mumuvmmheadless", # MuMu Player 12 VM
            "nemuheadless",    # MuMu/Nemu older
            "nemuplayer",      # MuMu/Nemu older
        ]

        def enum_callback(hwnd, results):
            if win32gui.IsWindowVisible(hwnd):
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    try:
                        process = psutil.Process(pid)
                        process_name = process.name().lower()
                        if any(name in process_name for name in EMULATOR_PROCESS_NAMES):
                            window_text = win32gui.GetWindowText(hwnd)
                            if window_text:
                                results[pid] = {
                                    "name": window_text,
                                    "process": process_name
                                }
                                print(f"Found emulator window: '{window_text}' (PID: {pid}, Process: {process_name})")
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                except:
                    pass

        win32gui.EnumWindows(enum_callback, emulator_windows)
        print(f"Total emulator windows found: {len(emulator_windows)}")

        device_map = {}
        print(f"Found devices: {devices}")

        for device in devices:
            try:
                if ":" in device:
                    port = device.split(":")[1]

                    for pid, info in emulator_windows.items():
                        try:
                            proc = psutil.Process(pid)
                            cmdline = " ".join(proc.cmdline())

                            # BlueStacks: port in command line args
                            if f"-adb-port {port}" in cmdline or f"--adb-port {port}" in cmdline:
                                device_map[device] = info["name"]
                                print(f"Mapped {device} -> {info['name']} (via port {port})")
                                break

                            # MuMu: port in command line args
                            if f"--adb_port {port}" in cmdline or f"-adb_port {port}" in cmdline:
                                device_map[device] = info["name"]
                                print(f"Mapped {device} -> {info['name']} (via port {port})")
                                break
                        except:
                            continue

                if device not in device_map:
                    device_map[device] = device
                    print(f"No window found for {device}, using device ID")
            except Exception as e:
                print(f"Error mapping {device}: {e}")
                device_map[device] = device

        return device_map

    except Exception as e:
        print(f"Failed to get emulator instances: {e}")
        return {d: d for d in devices}
