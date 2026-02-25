import subprocess
import platform

from config import adb_path, EMULATOR_PORTS
from botlog import get_logger

_log = get_logger("devices")

# ============================================================
# DEVICE DETECTION (cross-platform, multi-emulator)
# ============================================================

def auto_connect_emulators():
    """Try to adb-connect known emulator ports so they show up in 'adb devices'.

    MuMu Player 12 and some other emulators don't register with ADB automatically.
    This pings all known ports and connects any that respond.
    """
    all_ports = set()
    for ports in EMULATOR_PORTS.values():
        all_ports.update(ports)

    connected = []
    for port in sorted(all_ports):
        addr = f"127.0.0.1:{port}"
        try:
            result = subprocess.run(
                [adb_path, "connect", addr],
                capture_output=True, text=True, timeout=3
            )
            output = result.stdout.strip()
            # "connected to" or "already connected" means success
            if "connected" in output.lower():
                connected.append(addr)
                _log.debug("Connected: %s", addr)
        except (subprocess.TimeoutExpired, Exception):
            pass  # Port not listening, skip silently

    if connected:
        _log.info("Auto-connect found %d emulator(s)", len(connected))
    else:
        _log.info("Auto-connect: no emulators found on known ports")
    return connected

def get_devices():
    """Get list of all connected ADB devices, deduplicating emulator/IP pairs."""
    try:
        result = subprocess.run([adb_path, "devices"], capture_output=True, text=True, timeout=10)
        lines = result.stdout.strip().split('\n')[1:]  # Skip "List of devices attached"
        devices = [line.split()[0] for line in lines if line.strip() and 'device' in line]

        # Deduplicate: if both "emulator-5554" and "127.0.0.1:5555" exist,
        # they are the same instance. Keep only the emulator-XXXX form.
        emulator_ports = set()
        for d in devices:
            if d.startswith("emulator-"):
                emulator_ports.add(int(d.split("-")[1]) + 1)

        deduped = []
        for d in devices:
            if ":" in d and not d.startswith("emulator-"):
                port = int(d.split(":")[1])
                if port in emulator_ports:
                    _log.debug("Dedup: %s (duplicate of emulator port %d)", d, port)
                    continue
            deduped.append(d)

        _log.info("Found %d device(s): %s", len(deduped), ", ".join(deduped) if deduped else "(none)")
        return deduped
    except Exception as e:
        _log.error("Failed to get devices: %s", e)
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
    _log.debug("Found devices: %s", devices)
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
        _log.warning("pywin32/psutil not installed — using device IDs")
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
                                _log.debug("Found emulator window: '%s' (PID: %d, Process: %s)",
                                          window_text, pid, process_name)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                except Exception:
                    pass

        win32gui.EnumWindows(enum_callback, emulator_windows)
        _log.debug("Total emulator windows found: %d", len(emulator_windows))

        device_map = {}
        _log.debug("Found devices: %s", devices)

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
                                _log.debug("Mapped %s -> %s (via port %s)", device, info['name'], port)
                                break

                            # MuMu: port in command line args
                            if f"--adb_port {port}" in cmdline or f"-adb_port {port}" in cmdline:
                                device_map[device] = info["name"]
                                _log.debug("Mapped %s -> %s (via port %s)", device, info['name'], port)
                                break
                        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                            continue

                if device not in device_map:
                    device_map[device] = device
                    _log.debug("No window found for %s, using device ID", device)
            except Exception as e:
                _log.warning("Error mapping %s: %s", device, e)
                device_map[device] = device

        return device_map

    except Exception as e:
        _log.error("Failed to get emulator instances: %s", e)
        return {d: d for d in devices}
