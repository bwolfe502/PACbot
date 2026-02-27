"""
PACbot WebSocket Tunnel Client

Connects to a relay server and forwards proxied HTTP requests to the local
Flask dashboard at localhost:8080.  Runs in a daemon thread with its own
asyncio event loop.

Public API
----------
start_tunnel(relay_url, relay_secret, bot_name)
    Start the tunnel in a background daemon thread.
stop_tunnel()
    Signal the tunnel to disconnect and stop.
tunnel_status() -> str
    Return "connected", "connecting", "disconnected", or "disabled".
"""

import asyncio
import base64
import json
import logging
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

_log = logging.getLogger("tunnel")

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
_thread: threading.Thread | None = None
_stop_event = threading.Event()
_status = "disabled"          # disabled | connecting | connected | disconnected
_status_lock = threading.Lock()

RECONNECT_BASE = 5            # initial backoff seconds
RECONNECT_MAX = 60            # cap
LOCAL_URL = "http://127.0.0.1:8080"
EXECUTOR_WORKERS = 4
LOCAL_TIMEOUT = 25            # per-request timeout for local forwarding

# ---------------------------------------------------------------------------
# Local HTTP forwarding (runs in thread pool)
# ---------------------------------------------------------------------------

def _forward_to_local(msg: dict) -> dict:
    """Forward a proxied request to the local Flask dashboard."""
    path = msg.get("path", "/")
    url = LOCAL_URL + path
    method = msg.get("method", "GET")
    headers = msg.get("headers", {})
    body_b64 = msg.get("body_b64", "")
    body = base64.b64decode(body_b64) if body_b64 else None

    # Build request — only attach body for methods that use it
    req = urllib.request.Request(
        url,
        data=body if method in ("POST", "PUT", "PATCH") else None,
        method=method,
    )
    # Forward relevant headers
    for k, v in headers.items():
        lower = k.lower()
        if lower in ("host", "transfer-encoding", "connection"):
            continue
        try:
            req.add_header(k, v)
        except Exception:
            pass

    try:
        resp = urllib.request.urlopen(req, timeout=LOCAL_TIMEOUT)
        resp_body = resp.read()
        resp_headers = {k: v for k, v in resp.headers.items()
                        if k.lower() not in ("transfer-encoding", "connection")}
        return {
            "id": msg["id"],
            "status": resp.status,
            "headers": resp_headers,
            "body_b64": base64.b64encode(resp_body).decode("ascii"),
        }
    except urllib.error.HTTPError as e:
        resp_body = e.read() if e.fp else b""
        resp_headers = {k: v for k, v in e.headers.items()} if e.headers else {}
        return {
            "id": msg["id"],
            "status": e.code,
            "headers": resp_headers,
            "body_b64": base64.b64encode(resp_body).decode("ascii"),
        }
    except Exception as e:
        _log.debug("Local forward failed: %s", e)
        return {
            "id": msg["id"],
            "status": 502,
            "headers": {"Content-Type": "text/plain"},
            "body_b64": base64.b64encode(
                f"Local dashboard unreachable: {e}".encode()
            ).decode("ascii"),
        }

# ---------------------------------------------------------------------------
# Async tunnel loop
# ---------------------------------------------------------------------------

async def _handle_request(ws, msg: dict, executor: ThreadPoolExecutor) -> None:
    """Process one proxied request and send the response back."""
    loop = asyncio.get_event_loop()
    try:
        response = await loop.run_in_executor(executor, _forward_to_local, msg)
        await ws.send(json.dumps(response))
    except Exception as e:
        _log.debug("Failed to handle request %s: %s", msg.get("id", "?"), e)


async def _run_tunnel(relay_url: str, relay_secret: str, bot_name: str) -> None:
    global _status
    try:
        import websockets
    except ImportError:
        _log.error("websockets package not installed. Install with: pip install websockets")
        with _status_lock:
            _status = "disabled"
        return

    executor = ThreadPoolExecutor(max_workers=EXECUTOR_WORKERS)
    backoff = RECONNECT_BASE

    ws_url = f"{relay_url}?secret={relay_secret}&bot={bot_name}"

    while not _stop_event.is_set():
        with _status_lock:
            _status = "connecting"
        try:
            async with websockets.connect(
                ws_url,
                max_size=16 * 1024 * 1024,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                with _status_lock:
                    _status = "connected"
                _log.info("Tunnel connected to relay (bot=%s)", bot_name)
                backoff = RECONNECT_BASE  # reset on success

                async for raw_msg in ws:
                    if _stop_event.is_set():
                        break
                    try:
                        msg = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        _log.warning("Malformed message from relay, skipping")
                        continue
                    asyncio.ensure_future(_handle_request(ws, msg, executor))

        except Exception as e:
            _log.warning("Tunnel disconnected: %s — reconnecting in %ds", e, backoff)

        with _status_lock:
            _status = "disconnected"

        # Wait before reconnecting (check stop_event every second)
        for _ in range(backoff):
            if _stop_event.is_set():
                break
            await asyncio.sleep(1)
        backoff = min(backoff * 2, RECONNECT_MAX)

    with _status_lock:
        _status = "disabled"
    executor.shutdown(wait=False)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_tunnel(relay_url: str, relay_secret: str, bot_name: str) -> None:
    """Start the tunnel client in a daemon thread. Safe to call multiple times."""
    global _thread
    if _thread is not None and _thread.is_alive():
        _log.debug("Tunnel already running, ignoring start_tunnel()")
        return

    _stop_event.clear()

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run_tunnel(relay_url, relay_secret, bot_name))
        finally:
            loop.close()

    _thread = threading.Thread(target=_run, daemon=True, name="tunnel")
    _thread.start()
    _log.info("Tunnel thread started → %s (bot=%s)", relay_url, bot_name)


def stop_tunnel() -> None:
    """Signal the tunnel to disconnect and stop."""
    global _status
    _stop_event.set()
    with _status_lock:
        _status = "disabled"
    _log.info("Tunnel stop requested")


def tunnel_status() -> str:
    """Return current tunnel status string."""
    with _status_lock:
        return _status
