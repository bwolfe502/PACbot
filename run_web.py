"""9Bot — web-only entry point.

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


def _open_app_window(url, log):
    """Open a chromium-based browser in app mode (no URL bar/tabs).

    Tries Microsoft Edge first (always on Windows), then Chrome, then
    falls back to the default browser.
    """
    import shutil
    import subprocess

    # Chromium browsers to try — (label, candidates)
    # Edge is rarely on PATH; check common install locations directly.
    _edge_paths = [
        os.path.join(os.environ.get("ProgramFiles(x86)", ""),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    candidates = (
        [("Edge", p) for p in _edge_paths if os.path.isfile(p)]
        + [("Edge", "msedge"), ("Chrome", "chrome"),
           ("Chrome", "google-chrome")]
    )

    for name, cmd in candidates:
        path = cmd if os.path.isabs(cmd) else shutil.which(cmd)
        if path:
            try:
                # Dedicated user-data-dir forces a new process so
                # window-size flags aren't ignored by an existing Edge.
                app_data = os.path.join(os.environ.get("LOCALAPPDATA",
                                        os.path.expanduser("~")),
                                        "9Bot", "edge-app")
                subprocess.Popen([path, f"--app={url}",
                                  "--window-size=420,750",
                                  f"--user-data-dir={app_data}"])
                log.info("Opened %s in app mode", name)
                return
            except OSError:
                pass

    # Last resort: plain browser
    log.info("No Chromium browser found — opening default browser")
    import webbrowser
    webbrowser.open(url)


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
    # Relay tunnel (auto-configured from license key)
    # ------------------------------------------------------------------
    from startup import get_relay_config
    relay_cfg = get_relay_config(settings)
    if relay_cfg:
        _relay_url, _relay_secret, _relay_bot = relay_cfg
        try:
            from tunnel import start_tunnel
            start_tunnel(_relay_url, _relay_secret, _relay_bot)
            host = _relay_url.replace("ws://", "").replace("wss://", "").split("/")[0]
            log.info("Remote access: http://%s/%s/", host, _relay_bot)
        except ImportError:
            log.info("Remote access unavailable ('websockets' not installed)")
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
            title = f"9Bot v{get_current_version()}"

            # Set taskbar icon on Windows
            icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")

            def _set_icon():
                """Set window/taskbar icon via Windows API after window appears."""
                if sys.platform != "win32" or not os.path.isfile(icon_path):
                    return
                time.sleep(0.5)
                try:
                    import ctypes
                    user32 = ctypes.windll.user32
                    hwnd = user32.FindWindowW(None, title)
                    if not hwnd:
                        return
                    icon = user32.LoadImageW(
                        0, icon_path, 1, 0, 0, 0x0010 | 0x0040)
                    if icon:
                        user32.SendMessageW(hwnd, 0x0080, 0, icon)
                        user32.SendMessageW(hwnd, 0x0080, 1, icon)
                except Exception:
                    pass

            window = webview.create_window(title, url,
                                           width=520, height=900,
                                           min_size=(400, 600))
            config._quit_callback = window.destroy
            log.info("Opening native window...")
            print(f"\n  Dashboard: http://{local_ip}:8080  (phone access)\n")
            webview.start(func=_set_icon)  # blocks until window closed
            _on_exit()
            os._exit(0)  # force exit — daemon threads may linger
        except Exception as exc:
            log.warning("pywebview window failed (%s) — falling back to browser", exc)
            webview = None  # fall through to browser

    if webview is None:
        print(f"\n  Dashboard: http://{local_ip}:8080\n")
        # On restart, the existing browser window reconnects automatically —
        # don't open a duplicate.
        if os.environ.pop("NINEBOT_RESTART", None) or os.environ.pop("PACBOT_RESTART", None):
            log.info("Restart detected — reusing existing browser window")
        else:
            _open_app_window(url, log)
        try:
            print("Press Ctrl+C to stop 9Bot.\n")
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    _on_exit()


if __name__ == "__main__":
    main()
