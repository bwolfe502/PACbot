import subprocess
import platform

from config import adb_path, EMULATOR_PORTS
from botlog import get_logger

_log = get_logger("devices")

# ============================================================
# DEVICE DETECTION (cross-platform, multi-emulator)
# ============================================================

def auto_connect_emulators():
    """Try to adb-connect emulator ports so they show up in 'adb devices'.

    On Windows: inspects running emulator processes to discover their real ADB
    ports (BlueStacks assigns non-sequential ports like 5635 that aren't in
    any predictable range).  Only connects ports that ADB doesn't already see.

    On macOS/Linux: probes the well-known ports in EMULATOR_PORTS.
    """
    if platform.system() == "Windows":
        return _auto_connect_windows()

    return _auto_connect_by_ports()


def _auto_connect_windows():
    """Windows: find emulator ADB ports from running processes via psutil."""
    try:
        import psutil
    except ImportError:
        _log.debug("psutil not available — falling back to port scan")
        return _auto_connect_by_ports()

    # Collect ADB ports already known to the server
    existing = get_devices()
    known_ports = set()
    for d in existing:
        if d.startswith("emulator-"):
            try:
                known_ports.add(int(d.split("-")[1]) + 1)
            except (IndexError, ValueError):
                pass
        elif ":" in d:
            try:
                known_ports.add(int(d.split(":")[1]))
            except (IndexError, ValueError):
                pass

    # Process name patterns for supported emulators
    emu_names = ["hd-player", "bluestacks", "mumuplayer",
                 "mumuvmmheadless", "nemuheadless", "nemuplayer"]

    discovered_ports = set()
    for proc in psutil.process_iter(["pid", "name"]):
        pname = (proc.info["name"] or "").lower()
        if not any(n in pname for n in emu_names):
            continue
        try:
            for conn in proc.net_connections(kind="tcp4"):
                if conn.status == "LISTEN" and conn.laddr.ip == "127.0.0.1":
                    discovered_ports.add(conn.laddr.port)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass

    new_ports = discovered_ports - known_ports
    if not new_ports:
        if existing:
            _log.debug("Auto-connect: all %d emulator(s) already visible", len(existing))
        else:
            _log.info("Auto-connect: no running emulator processes found")
        return []

    _log.info("Discovered %d new emulator port(s): %s", len(new_ports),
              ", ".join(str(p) for p in sorted(new_ports)))
    return _connect_ports(new_ports)


def _auto_connect_by_ports():
    """macOS/Linux: probe well-known emulator ports from EMULATOR_PORTS."""
    all_ports = set()
    for ports in EMULATOR_PORTS.values():
        all_ports.update(ports)
    return _connect_ports(all_ports)


def _connect_ports(ports):
    """Try ``adb connect`` on each port, return list of successfully connected addresses."""
    connected = []
    for port in sorted(ports):
        addr = f"127.0.0.1:{port}"
        try:
            result = subprocess.run(
                [adb_path, "connect", addr],
                capture_output=True, text=True, timeout=3
            )
            output = result.stdout.strip()
            if "connected" in output.lower():
                connected.append(addr)
                _log.debug("Connected: %s", addr)
        except (subprocess.TimeoutExpired, Exception):
            pass

    if connected:
        _log.info("Auto-connect found %d emulator(s)", len(connected))
    else:
        _log.info("Auto-connect: no emulators found on probed ports")
    return connected

def get_devices():
    """Get list of all connected ADB devices, with duplicates removed.

    ADB can show the same emulator twice — e.g. ``emulator-5554`` (auto-registered)
    and ``127.0.0.1:5555`` (from ``adb connect``).  The convention is that
    ``emulator-N`` uses ADB port ``N+1``, so we drop any ``127.0.0.1:<port>``
    entry whose port matches an existing ``emulator-<port-1>`` entry.
    """
    try:
        result = subprocess.run([adb_path, "devices"], capture_output=True, text=True, timeout=10)
        lines = result.stdout.strip().split('\n')[1:]  # Skip "List of devices attached"
        raw = [line.split()[0] for line in lines if line.strip() and 'device' in line]

        # Build set of ADB ports claimed by emulator-N entries (port = N+1)
        emulator_ports = set()
        for d in raw:
            if d.startswith("emulator-"):
                try:
                    emulator_ports.add(int(d.split("-")[1]) + 1)
                except (IndexError, ValueError):
                    pass

        # Filter out 127.0.0.1:<port> duplicates
        devices = []
        for d in raw:
            if ":" in d and d.startswith("127.0.0.1:"):
                try:
                    port = int(d.split(":")[1])
                    if port in emulator_ports:
                        _log.debug("Dropping duplicate %s (same as emulator-%d)", d, port - 1)
                        continue
                except (IndexError, ValueError):
                    pass
            devices.append(d)

        _log.debug("Found %d device(s): %s", len(devices), ", ".join(devices) if devices else "(none)")
        return devices
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
    """Map ADB devices to emulator window names via network connections.

    For each emulator window PID, checks which port it LISTENs on, then
    matches that port to ADB device IDs (both ``127.0.0.1:port`` and
    ``emulator-N`` where port = N+1).
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
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                except Exception:
                    pass

        win32gui.EnumWindows(enum_callback, emulator_windows)

        # Build PID → ADB port mapping from network connections
        pid_to_port = {}
        for pid in emulator_windows:
            try:
                proc = psutil.Process(pid)
                for conn in proc.net_connections(kind="tcp4"):
                    if conn.status == "LISTEN" and conn.laddr.ip == "127.0.0.1":
                        pid_to_port[pid] = conn.laddr.port
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        # Build device → expected ADB port
        device_ports = {}
        for device in devices:
            if ":" in device:
                try:
                    device_ports[device] = int(device.split(":")[1])
                except (IndexError, ValueError):
                    pass
            elif device.startswith("emulator-"):
                try:
                    device_ports[device] = int(device.split("-")[1]) + 1
                except (IndexError, ValueError):
                    pass

        # Match devices to windows via port
        device_map = {}
        for device in devices:
            port = device_ports.get(device)
            if port:
                for pid, listen_port in pid_to_port.items():
                    if listen_port == port:
                        device_map[device] = emulator_windows[pid]["name"]
                        break
            if device not in device_map:
                device_map[device] = device

        mapped = {d: n for d, n in device_map.items() if n != d}
        if mapped:
            _log.debug("Window mapping: %s",
                       ", ".join(f"{d} -> {n}" for d, n in mapped.items()))

        return device_map

    except Exception as e:
        _log.error("Failed to get emulator instances: %s", e)
        return {d: d for d in devices}
