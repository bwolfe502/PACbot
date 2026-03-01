"""
PACbot Logging, Metrics, and Action Timing

Single module for all observability infrastructure:
- setup_logging()    — configure Python logging (call once at startup)
- get_logger()       — get a logger with optional device context
- StatsTracker       — thread-safe per-device metrics collection
- timed_action()     — decorator for automatic timing + stats + error screenshots
- stats              — global StatsTracker instance
"""

import logging
import logging.handlers
import os
import json
import time
import functools
from datetime import datetime
from threading import Lock

import psutil

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
STATS_DIR = os.path.join(SCRIPT_DIR, "stats")

# Version info — read once at import time
_VERSION_FILE = os.path.join(SCRIPT_DIR, "version.txt")
try:
    with open(_VERSION_FILE, "r") as _f:
        BOT_VERSION = _f.read().strip()
except Exception:
    BOT_VERSION = "unknown"

# ============================================================
# ADAPTIVE BUDGET CONFIGURATION
# ============================================================
# Controls how timed_wait() dynamically shortens sleep budgets
# based on observed transition times per device.  Starts at full
# budgets and gradually tightens as the bot learns each machine.

MIN_ADAPTIVE_SAMPLES = 8            # successful samples before adapting
MIN_ADAPTIVE_SUCCESS_RATE = 0.8     # condition-met rate to trust data
ADAPTIVE_HEADROOM = 1.3             # safety multiplier above P90
ADAPTIVE_FLOOR_FRACTION = 0.4       # never below 40% of original budget
ADAPTIVE_MIN_BUDGET_S = 0.3         # absolute floor in seconds

# Reference to the console handler so set_console_verbose() can adjust it
_console_handler = None

# ============================================================
# MEMORY MONITORING
# ============================================================

_process = psutil.Process(os.getpid())
_peak_memory_mb = 0.0

def get_memory_mb():
    """Return current process RSS in MB."""
    return _process.memory_info().rss / (1024 * 1024)


def get_peak_memory_mb():
    """Return the highest RSS observed by memory checkpoints."""
    return _peak_memory_mb


def _update_peak():
    """Update the high-water mark."""
    global _peak_memory_mb
    current = get_memory_mb()
    if current > _peak_memory_mb:
        _peak_memory_mb = current
    return current


# ============================================================
# LOGGING SETUP
# ============================================================

def setup_logging(verbose=False):
    """Configure Python logging. Call once at startup.

    Sets up:
    - Console handler: INFO normally, DEBUG when verbose
    - Rotating file handler: DEBUG always (full flight recorder)
    """
    global _console_handler

    os.makedirs(LOG_DIR, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Prevent duplicate handlers if called multiple times
    if root.handlers:
        return

    # Console handler — clean format matching current output style
    _console_handler = logging.StreamHandler()
    _console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_fmt = logging.Formatter(
        "[%(device)s] %(message)s",
        defaults={"device": "system"}
    )
    _console_handler.setFormatter(console_fmt)
    root.addHandler(_console_handler)

    # Rotating file handler — rich format for AI/human post-mortem analysis
    log_file = os.path.join(LOG_DIR, "pacbot.log")
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3,
        encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(device)s] %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        defaults={"device": "system"}
    )
    file_handler.setFormatter(file_fmt)
    root.addHandler(file_handler)

    # Suppress noisy third-party loggers
    logging.getLogger("easyocr").setLevel(logging.ERROR)
    logging.getLogger("PIL").setLevel(logging.WARNING)
    logging.getLogger("torch").setLevel(logging.WARNING)

    # Session start banner — makes each run easy to find in rotating log
    import platform as _plat
    _banner = logging.getLogger("botlog")
    _banner.info("=" * 60)
    _banner.info("NEW SESSION — %s — v%s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), BOT_VERSION)
    _banner.info("System: %s %s | %s | %d cores | Python %s",
                 _plat.system(), _plat.release(), _plat.machine(),
                 os.cpu_count() or 0, _plat.python_version())
    _banner.info("Memory: %.0f MB (startup)", _update_peak())
    _banner.info("=" * 60)


def set_console_verbose(verbose):
    """Toggle console verbosity at runtime (called by GUI toggle)."""
    if _console_handler is not None:
        _console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)


# ============================================================
# LOGGER HELPER
# ============================================================

def get_logger(module_name, device=None):
    """Get a logger, optionally bound to a device ID.

    Usage in device-aware functions:
        log = get_logger("actions", device)
        log.info("Rally joined!")
        # Console: [emulator-5584] Rally joined!
        # File:    2024-01-15 14:32:05.123 [emulator-5584] INFO  actions: Rally joined!

    Usage in non-device modules:
        log = get_logger("config")
        log.info("Min troops set to 0")
        # Console: [system] Min troops set to 0
    """
    logger = logging.getLogger(module_name)
    if device:
        return logging.LoggerAdapter(logger, {"device": device})
    return logging.LoggerAdapter(logger, {"device": "system"})


# ============================================================
# STATS TRACKER
# ============================================================

class StatsTracker:
    """Thread-safe per-device metrics for post-session analysis.

    Tracks action success/failure/timing, template match failures,
    navigation failures, and recent errors. Saves to JSON on shutdown.
    """

    AUTO_SAVE_INTERVAL = 300  # seconds (5 minutes)

    def __init__(self):
        self._lock = Lock()
        self._session_start = datetime.now()
        self._data = {}
        self._load_previous_session()
        self._start_auto_save()

    def _load_previous_session(self):
        """Seed transition_times from the most recent session file.

        Enables adaptive budgets to work immediately on session 2+
        using timing data accumulated in prior sessions.  Only loads
        transition_times — other metrics start fresh each session.
        """
        _log = logging.getLogger("botlog")
        try:
            if not os.path.isdir(STATS_DIR):
                return
            files = sorted(
                [f for f in os.listdir(STATS_DIR)
                 if f.startswith("session_") and f.endswith(".json")],
                key=lambda f: os.path.getmtime(os.path.join(STATS_DIR, f)),
                reverse=True,
            )
            if not files:
                return
            filepath = os.path.join(STATS_DIR, files[0])
            with open(filepath, "r") as f:
                prev = json.load(f)
            with self._lock:
                for device, device_data in prev.get("devices", {}).items():
                    prev_tt = device_data.get("transition_times", {})
                    if not prev_tt:
                        continue
                    self._ensure_device(device)
                    transitions = self._data[device]["transition_times"]
                    for label, info in prev_tt.items():
                        if label in transitions:
                            continue  # current session already has data
                        transitions[label] = {
                            "count": info.get("count", 0),
                            "met_count": info.get("met_count", 0),
                            "budgeted_s": info.get("budgeted_s", 0),
                            "samples": list(info.get("samples", [])),
                        }
            _log.info("Loaded transition data from previous session: %s", files[0])
        except Exception as e:
            _log.debug("Could not load previous session data: %s", e)

    def _start_auto_save(self):
        """Periodically save stats to disk so data isn't lost on crash/kill."""
        from threading import Timer

        def _tick():
            try:
                self.save()
                mem = _update_peak()
                _log = logging.getLogger("botlog")
                _log.info("Memory checkpoint: %.0f MB (peak: %.0f MB)", mem, _peak_memory_mb)
            except Exception:
                pass
            self._start_auto_save()

        self._auto_save_timer = Timer(self.AUTO_SAVE_INTERVAL, _tick)
        self._auto_save_timer.daemon = True
        self._auto_save_timer.start()

    def _ensure_device(self, device):
        if device not in self._data:
            self._data[device] = {
                "actions": {},
                "template_misses": {},
                "template_hits": {},
                "transition_times": {},
                "nav_failures": {},
                "errors": [],
                "adb_timing": {},
            }

    def record_action(self, device, action_name, success, duration_s, error_msg=None):
        """Record an action attempt with outcome and timing."""
        with self._lock:
            self._ensure_device(device)
            actions = self._data[device]["actions"]
            if action_name not in actions:
                actions[action_name] = {
                    "attempts": 0, "successes": 0, "failures": 0,
                    "total_time_s": 0.0, "last_failure": None
                }
            entry = actions[action_name]
            entry["attempts"] += 1
            entry["total_time_s"] = round(entry["total_time_s"] + duration_s, 1)
            if success:
                entry["successes"] += 1
            else:
                entry["failures"] += 1
                entry["last_failure"] = error_msg or "unknown"
                # Keep last 50 errors across all actions
                errors = self._data[device]["errors"]
                errors.append({
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "action": action_name,
                    "error": error_msg or "unknown",
                    "duration_s": round(duration_s, 1)
                })
                if len(errors) > 50:
                    errors[:] = errors[-50:]

    def record_template_miss(self, device, template_name, best_score=0.0):
        """Record a template match failure with the best score achieved."""
        with self._lock:
            self._ensure_device(device)
            misses = self._data[device]["template_misses"]
            if template_name not in misses:
                misses[template_name] = {"count": 0, "best_scores": []}
            entry = misses[template_name]
            entry["count"] += 1
            # Keep last 10 best scores for trend analysis
            entry["best_scores"].append(round(best_score, 3))
            if len(entry["best_scores"]) > 10:
                entry["best_scores"] = entry["best_scores"][-10:]

    def record_template_hit(self, device, template_name, x, y, confidence):
        """Record a successful template match with its position.

        Tracks min/max bounding box per template so regions can be
        tightened with real data from live sessions.
        """
        with self._lock:
            self._ensure_device(device)
            hits = self._data[device]["template_hits"]
            if template_name not in hits:
                hits[template_name] = {
                    "count": 0,
                    "min_x": x, "max_x": x,
                    "min_y": y, "max_y": y,
                    "recent": [],
                }
            entry = hits[template_name]
            entry["count"] += 1
            entry["min_x"] = min(entry["min_x"], x)
            entry["max_x"] = max(entry["max_x"], x)
            entry["min_y"] = min(entry["min_y"], y)
            entry["max_y"] = max(entry["max_y"], y)
            entry["recent"].append([x, y, round(confidence, 3)])
            if len(entry["recent"]) > 20:
                entry["recent"] = entry["recent"][-20:]

    def get_template_hit_bounds(self, device, template_name):
        """Return the observed bounding box for a template on a device.

        Returns (min_x, min_y, max_x, max_y, count) or None if no data.
        Coordinates are template center positions (not top-left).
        """
        with self._lock:
            if device not in self._data:
                return None
            entry = self._data[device]["template_hits"].get(template_name)
            if entry is None:
                return None
            return (entry["min_x"], entry["min_y"],
                    entry["max_x"], entry["max_y"], entry["count"])

    def record_nav_failure(self, device, from_screen, to_screen):
        """Record a navigation failure."""
        with self._lock:
            self._ensure_device(device)
            key = f"{from_screen}->{to_screen}"
            nav = self._data[device]["nav_failures"]
            nav[key] = nav.get(key, 0) + 1

    def record_adb_timing(self, device, command, elapsed_s, success=True):
        """Record ADB command timing for latency tracking."""
        with self._lock:
            self._ensure_device(device)
            timings = self._data[device]["adb_timing"]
            if command not in timings:
                timings[command] = {
                    "count": 0, "total_s": 0.0, "max_s": 0.0,
                    "slow_count": 0, "failures": 0,
                }
            entry = timings[command]
            entry["count"] += 1
            entry["total_s"] = round(entry["total_s"] + elapsed_s, 2)
            entry["max_s"] = round(max(entry["max_s"], elapsed_s), 2)
            if elapsed_s > 3.0:
                entry["slow_count"] += 1
            if not success:
                entry["failures"] += 1

    def record_transition_time(self, device, label, actual_s, budgeted_s, condition_met):
        """Record how long a UI transition actually took vs its sleep budget.
        Used by timed_wait() to gather data on which sleeps can be shortened."""
        with self._lock:
            self._ensure_device(device)
            transitions = self._data[device].setdefault("transition_times", {})
            if label not in transitions:
                transitions[label] = {
                    "count": 0,
                    "met_count": 0,
                    "budgeted_s": budgeted_s,
                    "samples": [],
                }
            entry = transitions[label]
            entry["count"] += 1
            if condition_met:
                entry["met_count"] += 1
                entry["samples"].append(round(actual_s, 3))
                if len(entry["samples"]) > 20:
                    entry["samples"] = entry["samples"][-20:]

    def _check_template_trends_unlocked(self, device, template_name):
        """Check if a template's best scores are trending toward failure (no lock).
        Returns a warning string if scores are drifting down, or None."""
        misses = self._data.get(device, {}).get("template_misses", {})
        entry = misses.get(template_name)
        if entry is None or len(entry["best_scores"]) < 5:
            return None
        scores = entry["best_scores"]
        # Compare recent half vs older half
        mid = len(scores) // 2
        old_avg = sum(scores[:mid]) / mid
        new_avg = sum(scores[mid:]) / (len(scores) - mid)
        if new_avg < old_avg - 0.05 and new_avg < 0.75:
            return (f"{template_name}: score trending down "
                    f"({old_avg:.0%} -> {new_avg:.0%}, {entry['count']} misses)")
        return None

    def check_template_trends(self, device, template_name):
        """Check if a template's best scores are trending toward failure.
        Returns a warning string if scores are drifting down, or None."""
        with self._lock:
            return self._check_template_trends_unlocked(device, template_name)

    def save(self):
        """Save stats to a timestamped JSON file. Auto-cleans old sessions."""
        os.makedirs(STATS_DIR, exist_ok=True)
        with self._lock:
            now = datetime.now()
            duration = (now - self._session_start).total_seconds() / 60.0

            # Compute avg_time_s for each action
            output_devices = {}
            for device, data in self._data.items():
                device_copy = {
                    "actions": {},
                    "template_misses": data["template_misses"],
                    "template_hits": data.get("template_hits", {}),
                    "transition_times": {},
                    "nav_failures": data["nav_failures"],
                    "errors": data["errors"],
                    "adb_timing": {},
                }
                for cmd, info in data.get("adb_timing", {}).items():
                    entry = dict(info)
                    entry["avg_s"] = round(
                        entry["total_s"] / max(1, entry["count"]), 3
                    )
                    device_copy["adb_timing"][cmd] = entry
                for action_name, info in data["actions"].items():
                    entry = dict(info)
                    entry["avg_time_s"] = round(
                        entry["total_time_s"] / max(1, entry["attempts"]), 1
                    )
                    device_copy["actions"][action_name] = entry
                for label, info in data.get("transition_times", {}).items():
                    entry = dict(info)
                    samples = info["samples"]
                    if samples:
                        entry["min_s"] = min(samples)
                        entry["max_s"] = max(samples)
                        entry["avg_s"] = round(sum(samples) / len(samples), 3)
                    device_copy["transition_times"][label] = entry
                output_devices[device] = device_copy

            output = {
                "version": BOT_VERSION,
                "session_start": self._session_start.strftime("%Y-%m-%d %H:%M:%S"),
                "session_end": now.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_minutes": round(duration, 1),
                "memory_mb": round(get_memory_mb(), 1),
                "peak_memory_mb": round(_peak_memory_mb, 1),
                "devices": output_devices,
            }

            filename = f"session_{self._session_start.strftime('%Y%m%d_%H%M%S')}.json"
            filepath = os.path.join(STATS_DIR, filename)
            try:
                with open(filepath, "w") as f:
                    json.dump(output, f, indent=2)
            except Exception as e:
                _log = logging.getLogger("botlog")
                _log.warning("Failed to save session stats: %s", e)

        # Clean old session files (keep last 30)
        try:
            files = sorted(
                [os.path.join(STATS_DIR, f) for f in os.listdir(STATS_DIR)
                 if f.startswith("session_") and f.endswith(".json")],
                key=os.path.getmtime
            )
            while len(files) > 30:
                os.remove(files.pop(0))
        except Exception as e:
            _log = logging.getLogger("botlog")
            _log.warning("Failed to clean old session files: %s", e)

    def summary(self):
        """Return a human/AI-readable summary of the session."""
        with self._lock:
            if not self._data:
                return "No activity recorded this session."

            lines = []
            duration = (datetime.now() - self._session_start).total_seconds() / 60.0
            lines.append(f"Session duration: {duration:.0f} minutes")
            lines.append(f"Memory: {get_memory_mb():.0f} MB (peak: {_peak_memory_mb:.0f} MB)")

            for device, data in self._data.items():
                lines.append(f"\n=== {device} ===")

                if data["actions"]:
                    for action, info in sorted(data["actions"].items()):
                        avg = info["total_time_s"] / max(1, info["attempts"])
                        rate = info["successes"] / max(1, info["attempts"]) * 100
                        lines.append(
                            f"  {action}: {info['successes']}/{info['attempts']} "
                            f"({rate:.0f}% success, avg {avg:.1f}s"
                            f"{', ' + str(info['failures']) + ' failed' if info['failures'] else ''})"
                        )

                if data["template_misses"]:
                    top = sorted(data["template_misses"].items(),
                                 key=lambda x: x[1]["count"], reverse=True)[:5]
                    miss_parts = []
                    for name, info in top:
                        avg_score = sum(info["best_scores"]) / max(1, len(info["best_scores"]))
                        miss_parts.append(f"{name}({info['count']}x, avg best {avg_score:.0%})")
                    lines.append(f"  Top template misses: {', '.join(miss_parts)}")

                if data.get("template_hits"):
                    top_hits = sorted(data["template_hits"].items(),
                                      key=lambda x: x[1]["count"], reverse=True)[:10]
                    hit_parts = []
                    for name, info in top_hits:
                        hit_parts.append(
                            f"{name}({info['count']}x, "
                            f"x:{info['min_x']}-{info['max_x']} "
                            f"y:{info['min_y']}-{info['max_y']})"
                        )
                    lines.append(f"  Template hit regions: {', '.join(hit_parts)}")

                if data.get("transition_times"):
                    lines.append("  Transition times (actual vs budget):")
                    for label, info in sorted(data["transition_times"].items()):
                        samples = info["samples"]
                        if samples:
                            avg = sum(samples) / len(samples)
                            waste = info["budgeted_s"] - avg
                            lines.append(
                                f"    {label}: {info['met_count']}/{info['count']} met, "
                                f"avg {avg:.2f}s / budget {info['budgeted_s']}s "
                                f"(~{waste:.2f}s wasted per call)")
                        else:
                            lines.append(
                                f"    {label}: 0/{info['count']} met "
                                f"(budget {info['budgeted_s']}s)")

                if data["nav_failures"]:
                    nav_parts = [f"{k}({v})" for k, v in
                                 sorted(data["nav_failures"].items(),
                                        key=lambda x: x[1], reverse=True)[:3]]
                    lines.append(f"  Nav failures: {', '.join(nav_parts)}")

                if data.get("adb_timing"):
                    adb_parts = []
                    for cmd, info in sorted(data["adb_timing"].items()):
                        avg = info["total_s"] / max(1, info["count"])
                        part = f"{cmd}: {info['count']}x, avg {avg:.2f}s, max {info['max_s']:.2f}s"
                        if info["slow_count"]:
                            part += f", {info['slow_count']} slow"
                        if info["failures"]:
                            part += f", {info['failures']} failed"
                        adb_parts.append(part)
                    lines.append(f"  ADB timing: {'; '.join(adb_parts)}")

                # Template score trend warnings
                for tpl_name in list(data.get("template_misses", {})):
                    warning = self._check_template_trends_unlocked(device, tpl_name)
                    if warning:
                        lines.append(f"  TREND WARNING: {warning}")

            return "\n".join(lines)


# Global instance
stats = StatsTracker()


# ============================================================
# TIMED ACTION DECORATOR
# ============================================================

def timed_action(action_name):
    """Decorator that logs entry/exit/timing and records stats.

    Expects `device` as the first positional argument of the decorated function.
    For functions where device is not the first arg (e.g. join_rally(rally_types, device)),
    add manual timing instead of using this decorator.

    Usage:
        @timed_action("rally_titan")
        def rally_titan(device):
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(device, *args, **kwargs):
            log = get_logger(func.__module__ or "unknown", device)
            log.info(">>> %s starting", action_name)
            mem_before = get_memory_mb()
            start = time.time()
            try:
                result = func(device, *args, **kwargs)
                elapsed = time.time() - start
                success = result is not False and result is not None
                mem_after = _update_peak()
                mem_delta = mem_after - mem_before
                mem_note = f" ({mem_delta:+.1f} MB, RSS: {mem_after:.0f} MB)" if abs(mem_delta) > 5 else ""
                if success:
                    log.info("<<< %s completed in %.1fs%s", action_name, elapsed, mem_note)
                else:
                    log.warning("<<< %s returned failure in %.1fs%s", action_name, elapsed, mem_note)
                stats.record_action(device, action_name, success, elapsed)
                return result
            except Exception as e:
                elapsed = time.time() - start
                log.error("<<< %s failed after %.1fs: %s", action_name, elapsed, e,
                          exc_info=True)
                stats.record_action(device, action_name, False, elapsed, str(e))
                # Auto-save debug screenshot on unexpected failure
                try:
                    from navigation import _save_debug_screenshot
                    _save_debug_screenshot(device, f"error_{action_name}")
                except Exception:
                    pass
                raise
        return wrapper
    return decorator
