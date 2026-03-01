"""PACbot — web-only entry point.

Starts the Flask dashboard in a pywebview native window.  Phone access via
``http://<LAN-IP>:8080`` works simultaneously.

If pywebview is not installed, falls back to opening the default browser.
"""

import os
import sys
import time
import atexit
import signal
import threading

from startup import initialize, shutdown, apply_settings
from botlog import get_logger


def main():
    settings = initialize()
    log = get_logger("run_web")

    # ------------------------------------------------------------------
    # Flask server (background thread)
    # ------------------------------------------------------------------
    from web.dashboard import create_app, get_local_ip, ensure_firewall_open
    import logging as _wlog
    _wlog.getLogger("werkzeug").setLevel(_wlog.WARNING)

    ensure_firewall_open(8080)

    app = create_app()

    def _run_flask():
        from werkzeug.serving import make_server
        import socket
        srv = make_server("0.0.0.0", 8080, app, threaded=True)
        srv.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.serve_forever()

    flask_thread = threading.Thread(target=_run_flask, daemon=True)
    flask_thread.start()

    local_ip = get_local_ip()
    log.info("Web dashboard: http://%s:8080", local_ip)

    # ------------------------------------------------------------------
    # Background dead-task cleanup (replaces tkinter window.after)
    # ------------------------------------------------------------------
    from web.dashboard import cleanup_dead_tasks
    import config

    def _cleanup_loop():
        while True:
            try:
                cleanup_dead_tasks()
            except Exception:
                pass
            time.sleep(3)

    threading.Thread(target=_cleanup_loop, daemon=True).start()

    # ------------------------------------------------------------------
    # Relay tunnel (optional)
    # ------------------------------------------------------------------
    if settings.get("relay_enabled", False):
        _relay_url = settings.get("relay_url", "")
        _relay_secret = settings.get("relay_secret", "")
        _relay_bot = settings.get("relay_bot_name", "")
        if _relay_url and _relay_secret and _relay_bot:
            try:
                from tunnel import start_tunnel
                start_tunnel(_relay_url, _relay_secret, _relay_bot)
                log.info("Relay tunnel started")
            except ImportError:
                log.info("Relay tunnel enabled but 'websockets' not installed.")
            except Exception as e:
                log.warning("Failed to start relay tunnel: %s", e)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    _shutting_down = threading.Event()

    def _on_exit():
        if not _shutting_down.is_set():
            _shutting_down.set()
            shutdown()

    atexit.register(_on_exit)

    def _signal_handler(sig, frame):
        _on_exit()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # ------------------------------------------------------------------
    # Native window (pywebview) or browser fallback
    # ------------------------------------------------------------------
    url = "http://127.0.0.1:8080"

    def _try_import_webview():
        """Try to import pywebview, auto-install on first run if missing."""
        try:
            import webview
            return webview
        except ImportError:
            pass
        # One-shot attempt to install pywebview
        log.info("pywebview not installed — attempting auto-install...")
        print("\n  Installing pywebview for native window (one-time)...\n")
        import subprocess as _sp
        try:
            _sp.check_call([sys.executable, "-m", "pip", "install",
                            "pywebview", "-q"],
                           stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                           timeout=120)
            import webview
            return webview
        except Exception as exc:
            log.info("pywebview install failed (%s) — using browser fallback", exc)
            return None

    webview = _try_import_webview()

    if webview is not None:
        try:
            from updater import get_current_version
            title = f"PACbot v{get_current_version()}"

            window = webview.create_window(title, url,
                                           width=520, height=900,
                                           min_size=(400, 600))
            log.info("Opening native window...")
            print(f"\n  Dashboard: http://{local_ip}:8080  (phone access)\n")
            webview.start()  # blocks until window closed
        except Exception as exc:
            log.warning("pywebview window failed (%s) — falling back to browser", exc)
            webview = None  # fall through to browser

    if webview is None:
        print(f"\n  Dashboard: http://{local_ip}:8080\n")
        import webbrowser
        webbrowser.open(url)
        try:
            print("Press Ctrl+C to stop PACbot.\n")
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    _on_exit()


if __name__ == "__main__":
    main()
