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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
STATS_DIR = os.path.join(SCRIPT_DIR, "stats")

# Reference to the console handler so set_console_verbose() can adjust it
_console_handler = None


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
    _banner = logging.getLogger("botlog")
    _banner.info("=" * 60)
    _banner.info("NEW SESSION — %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
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

    def __init__(self):
        self._lock = Lock()
        self._session_start = datetime.now()
        self._data = {}

    def _ensure_device(self, device):
        if device not in self._data:
            self._data[device] = {
                "actions": {},
                "template_misses": {},
                "nav_failures": {},
                "errors": [],
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

    def record_nav_failure(self, device, from_screen, to_screen):
        """Record a navigation failure."""
        with self._lock:
            self._ensure_device(device)
            key = f"{from_screen}->{to_screen}"
            nav = self._data[device]["nav_failures"]
            nav[key] = nav.get(key, 0) + 1

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
                    "nav_failures": data["nav_failures"],
                    "errors": data["errors"],
                }
                for action_name, info in data["actions"].items():
                    entry = dict(info)
                    entry["avg_time_s"] = round(
                        entry["total_time_s"] / max(1, entry["attempts"]), 1
                    )
                    device_copy["actions"][action_name] = entry
                output_devices[device] = device_copy

            output = {
                "session_start": self._session_start.strftime("%Y-%m-%d %H:%M:%S"),
                "session_end": now.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_minutes": round(duration, 1),
                "devices": output_devices,
            }

            filename = f"session_{self._session_start.strftime('%Y%m%d_%H%M%S')}.json"
            filepath = os.path.join(STATS_DIR, filename)
            try:
                with open(filepath, "w") as f:
                    json.dump(output, f, indent=2)
            except Exception:
                pass

        # Clean old session files (keep last 30)
        try:
            files = sorted(
                [os.path.join(STATS_DIR, f) for f in os.listdir(STATS_DIR)
                 if f.startswith("session_") and f.endswith(".json")],
                key=os.path.getmtime
            )
            while len(files) > 30:
                os.remove(files.pop(0))
        except Exception:
            pass

    def summary(self):
        """Return a human/AI-readable summary of the session."""
        with self._lock:
            if not self._data:
                return "No activity recorded this session."

            lines = []
            duration = (datetime.now() - self._session_start).total_seconds() / 60.0
            lines.append(f"Session duration: {duration:.0f} minutes")

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

                if data["nav_failures"]:
                    nav_parts = [f"{k}({v})" for k, v in
                                 sorted(data["nav_failures"].items(),
                                        key=lambda x: x[1], reverse=True)[:3]]
                    lines.append(f"  Nav failures: {', '.join(nav_parts)}")

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
            start = time.time()
            try:
                result = func(device, *args, **kwargs)
                elapsed = time.time() - start
                success = result is not False and result is not None
                if success:
                    log.info("<<< %s completed in %.1fs", action_name, elapsed)
                else:
                    log.warning("<<< %s returned failure in %.1fs", action_name, elapsed)
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
