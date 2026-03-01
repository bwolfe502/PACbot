"""
PACbot WebSocket Relay Server

Standalone aiohttp server deployed on a DigitalOcean Droplet (or any VPS).
Accepts WebSocket connections from PACbot instances and proxies HTTP requests
from browsers to the appropriate bot.

Supports streaming responses (MJPEG) via stream_start/stream_chunk/stream_end
protocol messages.

Usage:
    RELAY_SECRET=your-secret python relay_server.py
    RELAY_SECRET=your-secret RELAY_PORT=8080 python relay_server.py

Environment variables:
    RELAY_SECRET  — shared secret for authenticating bot connections (required)
    RELAY_PORT    — HTTP port to listen on (default: 80)
"""

import asyncio
import base64
import json
import logging
import os
import uuid

from aiohttp import web, WSMsgType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("relay")

SHARED_SECRET = os.environ.get("RELAY_SECRET", "")
RELAY_PORT = int(os.environ.get("RELAY_PORT", "80"))
REQUEST_TIMEOUT = 30  # seconds
STREAM_CHUNK_TIMEOUT = 10  # seconds between stream chunks before giving up

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
# {bot_name: websocket}
_bots: dict[str, web.WebSocketResponse] = {}
# {bot_name: {request_id: asyncio.Future}}  — per-bot pending requests
_pending: dict[str, dict[str, asyncio.Future]] = {}
# {bot_name: {request_id: asyncio.Queue}}  — per-bot active streams
_streams: dict[str, dict[str, asyncio.Queue]] = {}

# ---------------------------------------------------------------------------
# HTML pages (inline, no external files needed)
# ---------------------------------------------------------------------------
_STYLE = """
body { background: #0c0c18; color: #e0e0f0; font-family: -apple-system, system-ui, sans-serif;
       display: flex; flex-direction: column; align-items: center; justify-content: center;
       min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box; }
h1 { font-size: 1.6em; margin-bottom: 0.3em; }
.bot-list { list-style: none; padding: 0; width: 100%; max-width: 400px; }
.bot-list li { margin: 8px 0; }
.bot-list a { display: flex; align-items: center; gap: 10px; padding: 14px 18px;
              background: #1a1a2e; border-radius: 12px; color: #e0e0f0;
              text-decoration: none; font-size: 1.1em; transition: background 0.15s; }
.bot-list a:hover { background: #252545; }
.dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.dot.online { background: #4cff8e; box-shadow: 0 0 6px #4cff8e; }
.dot.offline { background: #555; }
.muted { color: #667; font-size: 0.85em; }
"""


def _landing_page() -> str:
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>PACbot Relay</title><style>{_STYLE}</style>"
        f"</head><body><h1>PACbot Relay</h1>"
        f'<p class="muted">Use your PACbot dashboard to find your remote URL.</p>'
        f"</body></html>"
    )


def _offline_page(bot_name: str) -> str:
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{bot_name} — Offline</title><style>{_STYLE}</style>"
        f"<meta http-equiv='refresh' content='10'>"
        f"</head><body>"
        f"<h1>{bot_name}</h1>"
        f'<p><span class="dot offline" style="display:inline-block;vertical-align:middle"></span>'
        f" &nbsp;Offline</p>"
        f'<p class="muted">This page will auto-refresh when the bot reconnects.</p>'
        f"</body></html>"
    )

# ---------------------------------------------------------------------------
# WebSocket handler (bot connections)
# ---------------------------------------------------------------------------

async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    # Accept secret from Authorization header (preferred) or query param (legacy)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        secret = auth_header[7:]
    else:
        secret = request.query.get("secret", "")
    bot_name = request.query.get("bot", "").strip()

    if not SHARED_SECRET:
        log.error("RELAY_SECRET not set — rejecting all connections")
        raise web.HTTPForbidden(text="Server misconfigured: no secret set")

    if secret != SHARED_SECRET:
        log.warning("Rejected connection: bad secret")
        raise web.HTTPForbidden(text="Invalid secret")

    if not bot_name:
        log.warning("Rejected connection: no bot name")
        raise web.HTTPBadRequest(text="Missing 'bot' query parameter")

    ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)

    # Close old connection for this bot name if any
    old_ws = _bots.get(bot_name)
    if old_ws is not None and not old_ws.closed:
        log.info("Replacing existing connection for bot '%s'", bot_name)
        await old_ws.close()
    _cancel_pending(bot_name, "Bot reconnected")
    _cancel_all_streams(bot_name)

    _bots[bot_name] = ws
    _pending[bot_name] = {}
    _streams[bot_name] = {}
    log.info("Bot '%s' connected from %s", bot_name, request.remote)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    log.warning("Malformed JSON from bot '%s'", bot_name)
                    continue

                req_id = data.get("id")
                stream_type = data.get("stream")

                if stream_type:
                    # Streaming protocol message
                    if stream_type == "start":
                        # Create stream queue and resolve pending future
                        queue: asyncio.Queue = asyncio.Queue()
                        _streams.setdefault(bot_name, {})[req_id] = queue
                        fut = _pending.get(bot_name, {}).get(req_id)
                        if fut and not fut.done():
                            fut.set_result(data)
                    elif stream_type == "chunk":
                        queue = _streams.get(bot_name, {}).get(req_id)
                        if queue:
                            body = base64.b64decode(data.get("body_b64", ""))
                            await queue.put(body)
                    elif stream_type == "end":
                        queue = _streams.get(bot_name, {}).get(req_id)
                        if queue:
                            await queue.put(None)  # sentinel
                        _streams.get(bot_name, {}).pop(req_id, None)
                elif req_id and req_id in _pending.get(bot_name, {}):
                    # Normal request-response
                    _pending[bot_name][req_id].set_result(data)
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        if _bots.get(bot_name) is ws:
            del _bots[bot_name]
        _cancel_pending(bot_name, "Bot disconnected")
        _cancel_all_streams(bot_name)
        log.info("Bot '%s' disconnected", bot_name)

    return ws


def _cancel_pending(bot_name: str, reason: str) -> None:
    pending = _pending.pop(bot_name, {})
    for fut in pending.values():
        if not fut.done():
            fut.set_result({
                "status": 502,
                "headers": {"Content-Type": "text/plain"},
                "body_b64": base64.b64encode(reason.encode()).decode("ascii"),
            })


def _cancel_all_streams(bot_name: str) -> None:
    """Cancel all active streams for a bot (push end sentinels)."""
    streams = _streams.pop(bot_name, {})
    for queue in streams.values():
        try:
            queue.put_nowait(None)
        except Exception:
            pass


async def _send_cancel_stream(bot_name: str, req_id: str) -> None:
    """Tell the bot to stop a stream."""
    ws = _bots.get(bot_name)
    if ws and not ws.closed:
        try:
            await ws.send_json({"cancel_stream": req_id})
        except Exception:
            pass
    # Clean up stream queue
    queue = _streams.get(bot_name, {}).pop(req_id, None)
    if queue:
        try:
            queue.put_nowait(None)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# HTTP handler (browser requests)
# ---------------------------------------------------------------------------

async def handle_http(request: web.Request) -> web.StreamResponse:
    path = request.path

    # Landing page
    if path == "/" or path == "":
        return web.Response(text=_landing_page(), content_type="text/html")

    # Extract bot name from first path segment: /<bot_name>/...
    parts = path.strip("/").split("/", 1)
    bot_name = parts[0]
    sub_path = "/" + parts[1] if len(parts) > 1 else "/"

    # Redirect /<bot_name> to /<bot_name>/ for consistency
    if not path.endswith("/") and len(parts) == 1:
        raise web.HTTPFound(f"/{bot_name}/")

    ws = _bots.get(bot_name)
    if ws is None or ws.closed:
        return web.Response(text=_offline_page(bot_name), content_type="text/html")

    # Build request envelope
    req_id = str(uuid.uuid4())
    body = await request.read()
    query = request.query_string
    forward_path = sub_path
    if query:
        forward_path += "?" + query

    envelope = {
        "id": req_id,
        "method": request.method,
        "path": forward_path,
        "headers": {k: v for k, v in request.headers.items()
                    if k.lower() not in ("host", "transfer-encoding")},
        "body_b64": base64.b64encode(body).decode("ascii") if body else "",
    }

    # Send to bot and wait for response
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    _pending.setdefault(bot_name, {})[req_id] = future

    try:
        await ws.send_json(envelope)
    except Exception as e:
        _pending.get(bot_name, {}).pop(req_id, None)
        log.warning("Failed to send to bot '%s': %s", bot_name, e)
        return web.Response(text=_offline_page(bot_name), content_type="text/html")

    try:
        result = await asyncio.wait_for(future, timeout=REQUEST_TIMEOUT)
    except asyncio.TimeoutError:
        _pending.get(bot_name, {}).pop(req_id, None)
        return web.Response(text="Gateway Timeout", status=504)
    finally:
        _pending.get(bot_name, {}).pop(req_id, None)

    # Check if this is a streaming response
    if result.get("stream") == "start":
        return await _handle_stream_response(request, bot_name, req_id, result)

    # Build normal response
    resp_body = base64.b64decode(result.get("body_b64", ""))
    resp_headers = result.get("headers", {})
    # Filter headers that aiohttp manages itself
    for h in ("Transfer-Encoding", "Content-Length", "Content-Encoding"):
        resp_headers.pop(h, None)
        resp_headers.pop(h.lower(), None)

    # Rewrite absolute URLs in HTML responses to include bot prefix
    content_type = resp_headers.get("Content-Type", resp_headers.get("content-type", ""))
    if "text/html" in content_type:
        try:
            html = resp_body.decode("utf-8")
            p = f"/{bot_name}"
            # HTML attributes
            html = html.replace('href="/', f'href="{p}/')
            html = html.replace("href='/", f"href='{p}/")
            html = html.replace('src="/', f'src="{p}/')
            html = html.replace("src='/", f"src='{p}/")
            html = html.replace('action="/', f'action="{p}/')
            html = html.replace("action='/", f"action='{p}/")
            # JS fetch/redirect URLs
            html = html.replace("fetch('/", f"fetch('{p}/")
            html = html.replace('fetch("/', f'fetch("{p}/')
            html = html.replace("location='/", f"location='{p}/")
            html = html.replace('location="/', f'location="{p}/')
            html = html.replace("location.href='/", f"location.href='{p}/")
            html = html.replace('location.href="/', f'location.href="{p}/')
            html = html.replace("window.location='/", f"window.location='{p}/")
            html = html.replace('window.location="/', f'window.location="{p}/')
            resp_body = html.encode("utf-8")
        except Exception:
            pass  # if decode fails, send original body

    # Rewrite Location header for redirects
    location = resp_headers.get("Location", resp_headers.get("location", ""))
    if location and location.startswith("/") and not location.startswith(f"/{bot_name}/"):
        resp_headers["Location"] = f"/{bot_name}{location}"

    return web.Response(
        body=resp_body,
        status=result.get("status", 200),
        headers=resp_headers,
    )


async def _handle_stream_response(
    request: web.Request,
    bot_name: str,
    req_id: str,
    start_msg: dict,
) -> web.StreamResponse:
    """Forward a streaming response (MJPEG) from bot to browser."""
    resp_headers = start_msg.get("headers", {})
    # Filter managed headers
    for h in ("Transfer-Encoding", "Content-Length", "Content-Encoding"):
        resp_headers.pop(h, None)
        resp_headers.pop(h.lower(), None)

    resp = web.StreamResponse(status=start_msg.get("status", 200))
    for k, v in resp_headers.items():
        resp.headers[k] = v
    await resp.prepare(request)

    queue = _streams.get(bot_name, {}).get(req_id)
    if not queue:
        return resp

    try:
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(),
                                                timeout=STREAM_CHUNK_TIMEOUT)
            except asyncio.TimeoutError:
                break
            if chunk is None:  # end sentinel
                break
            await resp.write(chunk)
    except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
        pass
    finally:
        # Browser disconnected or stream ended — tell bot to stop
        await _send_cancel_stream(bot_name, req_id)

    try:
        await resp.write_eof()
    except Exception:
        pass
    return resp

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/ws/tunnel", handle_ws)
    app.router.add_route("*", "/{path_info:.*}", handle_http)
    return app


if __name__ == "__main__":
    if not SHARED_SECRET:
        print("ERROR: Set the RELAY_SECRET environment variable before starting.")
        print("  Example: RELAY_SECRET=my-secret-here python relay_server.py")
        raise SystemExit(1)
    log.info("Starting relay on port %d", RELAY_PORT)
    web.run_app(create_app(), host="0.0.0.0", port=RELAY_PORT)
