"""
9Bot WebSocket Relay Server

Standalone aiohttp server deployed on a DigitalOcean Droplet (or any VPS).
Accepts WebSocket connections from 9Bot instances and proxies HTTP requests
from browsers to the appropriate bot.

Supports streaming responses (MJPEG) via stream_start/stream_chunk/stream_end
protocol messages.

Bug report uploads are accepted via POST /_upload and stored on disk.
Admin interface at GET /_admin for browsing/downloading/deleting uploads.

Usage:
    RELAY_SECRET=your-secret python relay_server.py
    RELAY_SECRET=your-secret RELAY_PORT=8080 python relay_server.py

Environment variables:
    RELAY_SECRET  — shared secret for authenticating bot connections (required)
    RELAY_PORT    — HTTP port to listen on (default: 80)
    UPLOAD_DIR    — directory for bug report uploads (default: /opt/9bot-relay/uploads)
"""

import asyncio
import base64
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from aiohttp import web, WSMsgType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("relay")

SHARED_SECRET = os.environ.get("RELAY_SECRET", "")
RELAY_PORT = int(os.environ.get("RELAY_PORT", "80"))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/opt/9bot-relay/uploads")
REQUEST_TIMEOUT = 30  # seconds
STREAM_CHUNK_TIMEOUT = 10  # seconds between stream chunks before giving up
MAX_UPLOAD_SIZE = 150 * 1024 * 1024  # 150 MB
MAX_UPLOADS_PER_BOT = 10  # keep last N per bot

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
        f"<title>9Bot Relay</title><style>{_STYLE}</style>"
        f"</head><body><h1>9Bot Relay</h1>"
        f'<p class="muted">Use your 9Bot dashboard to find your remote URL.</p>'
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
# Bug report upload + admin
# ---------------------------------------------------------------------------

def _check_secret(request: web.Request) -> None:
    """Validate Bearer token or query-param secret. Raises 403 on failure."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        secret = auth_header[7:]
    else:
        secret = request.query.get("secret", "")
    if not SHARED_SECRET or secret != SHARED_SECRET:
        raise web.HTTPForbidden(text="Invalid secret")


def _safe_bot_name(name: str) -> str:
    """Validate and return a sanitized bot name. Raises 400 on invalid."""
    name = name.strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        raise web.HTTPBadRequest(text="Invalid bot name")
    return name


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _prune_uploads(bot_dir: str) -> None:
    """Keep only the newest MAX_UPLOADS_PER_BOT zip files in a bot directory."""
    try:
        zips = [f for f in os.listdir(bot_dir)
                if f.endswith(".zip") and os.path.isfile(os.path.join(bot_dir, f))]
    except OSError:
        return
    if len(zips) <= MAX_UPLOADS_PER_BOT:
        return
    zips.sort(key=lambda f: os.path.getmtime(os.path.join(bot_dir, f)))
    for old in zips[: len(zips) - MAX_UPLOADS_PER_BOT]:
        try:
            os.remove(os.path.join(bot_dir, old))
            log.info("Pruned old upload: %s/%s", os.path.basename(bot_dir), old)
        except OSError:
            pass


async def handle_upload(request: web.Request) -> web.Response:
    """Accept a bug report zip upload from a bot."""
    _check_secret(request)
    bot_name = _safe_bot_name(request.query.get("bot", ""))

    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "file":
        raise web.HTTPBadRequest(text="Missing 'file' field")

    bot_dir = os.path.join(UPLOAD_DIR, bot_name)
    os.makedirs(bot_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(bot_dir, f"bugreport_{timestamp}.zip")

    size = 0
    try:
        with open(dest, "wb") as f:
            while True:
                chunk = await field.read_chunk(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_SIZE:
                    raise web.HTTPRequestEntityTooLarge(
                        text=f"Upload exceeds {MAX_UPLOAD_SIZE // (1024*1024)} MB limit")
                f.write(chunk)
    except web.HTTPRequestEntityTooLarge:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise

    _prune_uploads(bot_dir)
    log.info("Upload from '%s': %s (%s)", bot_name,
             os.path.basename(dest), _format_size(size))
    return web.json_response({"status": "ok", "size": size,
                              "file": os.path.basename(dest)})


# ---------------------------------------------------------------------------
# Admin interface — browse / download / delete uploads
# ---------------------------------------------------------------------------

def _admin_page(body_html: str, title: str = "9Bot Admin") -> str:
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title><style>{_STYLE}"
        f".card {{ background:#1a1a2e; border-radius:12px; padding:16px 20px;"
        f" margin:10px 0; width:100%; max-width:600px; }}"
        f".file-row {{ display:flex; align-items:center; justify-content:space-between;"
        f" padding:8px 0; border-bottom:1px solid #252545; gap:10px; }}"
        f".file-row:last-child {{ border-bottom:none; }}"
        f".file-info {{ flex:1; min-width:0; }}"
        f".file-name {{ font-size:0.95em; word-break:break-all; }}"
        f".file-meta {{ color:#667; font-size:0.8em; }}"
        f".btn {{ padding:6px 14px; border-radius:8px; border:none; cursor:pointer;"
        f" font-size:0.85em; text-decoration:none; display:inline-block; }}"
        f".btn-dl {{ background:#2a4a6e; color:#e0e0f0; }}"
        f".btn-dl:hover {{ background:#3a5a8e; }}"
        f".btn-del {{ background:#5a2a2a; color:#e0e0f0; }}"
        f".btn-del:hover {{ background:#7a3a3a; }}"
        f".btn-back {{ background:#252545; color:#e0e0f0; margin-bottom:12px; }}"
        f".btn-back:hover {{ background:#353565; }}"
        f".total {{ color:#667; font-size:0.85em; margin-top:4px; }}"
        f"</style></head><body style='justify-content:flex-start;padding-top:40px'>"
        f"<h1>{title}</h1>{body_html}</body></html>"
    )


async def handle_admin(request: web.Request) -> web.Response:
    """Admin landing — list all bots with uploads."""
    _check_secret(request)
    secret_q = f"?secret={request.query.get('secret', '')}"

    if not os.path.isdir(UPLOAD_DIR):
        return web.Response(text=_admin_page(
            '<p class="muted">No uploads yet.</p>'), content_type="text/html")

    bots = []
    total_size = 0
    for name in sorted(os.listdir(UPLOAD_DIR)):
        bot_dir = os.path.join(UPLOAD_DIR, name)
        if not os.path.isdir(bot_dir):
            continue
        zips = [f for f in os.listdir(bot_dir) if f.endswith(".zip")]
        if not zips:
            continue
        dir_size = sum(os.path.getsize(os.path.join(bot_dir, f)) for f in zips)
        total_size += dir_size
        online = name in _bots and not _bots[name].closed
        bots.append((name, len(zips), dir_size, online))

    if not bots:
        return web.Response(text=_admin_page(
            '<p class="muted">No uploads yet.</p>'), content_type="text/html")

    rows = []
    for name, count, size, online in bots:
        dot = "online" if online else "offline"
        rows.append(
            f'<a href="/_admin/uploads/{name}{secret_q}">'
            f'<span class="dot {dot}"></span>'
            f'{name} &mdash; {count} file{"s" if count != 1 else ""}, '
            f'{_format_size(size)}</a>'
        )

    html = (
        f'<ul class="bot-list">{"".join(f"<li>{r}</li>" for r in rows)}</ul>'
        f'<p class="total">Total: {_format_size(total_size)}</p>'
    )
    return web.Response(text=_admin_page(html), content_type="text/html")


async def handle_admin_bot(request: web.Request) -> web.Response:
    """List uploads for a specific bot."""
    _check_secret(request)
    bot_name = _safe_bot_name(request.match_info["bot_name"])
    secret_q = f"?secret={request.query.get('secret', '')}"

    bot_dir = os.path.join(UPLOAD_DIR, bot_name)
    if not os.path.isdir(bot_dir):
        raise web.HTTPNotFound(text="No uploads for this bot")

    zips = [f for f in os.listdir(bot_dir)
            if f.endswith(".zip") and os.path.isfile(os.path.join(bot_dir, f))]
    zips.sort(key=lambda f: os.path.getmtime(os.path.join(bot_dir, f)), reverse=True)

    if not zips:
        raise web.HTTPNotFound(text="No uploads for this bot")

    rows = []
    for fname in zips:
        fpath = os.path.join(bot_dir, fname)
        size = os.path.getsize(fpath)
        mtime = datetime.fromtimestamp(os.path.getmtime(fpath), tz=timezone.utc)
        age = datetime.now(timezone.utc) - mtime
        if age.days > 0:
            age_str = f"{age.days}d ago"
        elif age.seconds >= 3600:
            age_str = f"{age.seconds // 3600}h ago"
        else:
            age_str = f"{max(1, age.seconds // 60)}m ago"

        dl_url = f"/_admin/uploads/{bot_name}/{fname}{secret_q}"
        del_url = dl_url
        rows.append(
            f'<div class="file-row">'
            f'<div class="file-info">'
            f'<div class="file-name">{fname}</div>'
            f'<div class="file-meta">{_format_size(size)} &middot; {age_str}</div>'
            f'</div>'
            f'<a class="btn btn-dl" href="{dl_url}">Download</a>'
            f'<button class="btn btn-del" onclick="del_file(this,\'{fname}\')">'
            f'Delete</button></div>'
        )

    del_all_js = (
        f"function del_file(btn,f){{if(!confirm('Delete '+f+'?'))return;"
        f"fetch('/_admin/uploads/{bot_name}/'+f+'{secret_q}',"
        f"{{method:'DELETE'}}).then(r=>{{if(r.ok)btn.closest('.file-row').remove();"
        f"else alert('Delete failed: '+r.status)}}).catch(e=>alert(e))}}"
        f"function del_all(){{if(!confirm('Delete ALL uploads for {bot_name}?'))return;"
        f"fetch('/_admin/uploads/{bot_name}{secret_q}',"
        f"{{method:'DELETE'}}).then(r=>{{if(r.ok)location.href='/_admin{secret_q}';"
        f"else alert('Failed: '+r.status)}}).catch(e=>alert(e))}}"
    )

    html = (
        f'<a class="btn btn-back" href="/_admin{secret_q}">&larr; All bots</a>'
        f'<div class="card"><h3 style="margin:0 0 10px">{bot_name}</h3>'
        f'{"".join(rows)}'
        f'<div style="margin-top:12px;text-align:right">'
        f'<button class="btn btn-del" onclick="del_all()">Delete All</button>'
        f'</div></div>'
        f'<script>{del_all_js}</script>'
    )
    return web.Response(text=_admin_page(html, f"{bot_name} — Uploads"),
                        content_type="text/html")


async def handle_admin_file(request: web.Request) -> web.Response:
    """Download or delete a specific upload."""
    _check_secret(request)
    bot_name = _safe_bot_name(request.match_info["bot_name"])
    filename = request.match_info["filename"]

    # Sanitize filename
    if "/" in filename or "\\" in filename or ".." in filename:
        raise web.HTTPBadRequest(text="Invalid filename")
    if not filename.endswith(".zip"):
        raise web.HTTPBadRequest(text="Only .zip files")

    fpath = os.path.join(UPLOAD_DIR, bot_name, filename)
    if not os.path.isfile(fpath):
        raise web.HTTPNotFound(text="File not found")

    if request.method == "DELETE":
        os.remove(fpath)
        log.info("Admin deleted: %s/%s", bot_name, filename)
        return web.json_response({"status": "ok", "deleted": filename})

    # GET — download
    return web.FileResponse(fpath, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
    })


async def handle_admin_delete_bot(request: web.Request) -> web.Response:
    """Delete all uploads for a bot."""
    _check_secret(request)
    bot_name = _safe_bot_name(request.match_info["bot_name"])
    bot_dir = os.path.join(UPLOAD_DIR, bot_name)

    if not os.path.isdir(bot_dir):
        raise web.HTTPNotFound(text="No uploads for this bot")

    count = 0
    for f in os.listdir(bot_dir):
        fpath = os.path.join(bot_dir, f)
        if os.path.isfile(fpath) and f.endswith(".zip"):
            os.remove(fpath)
            count += 1
    try:
        os.rmdir(bot_dir)
    except OSError:
        pass

    log.info("Admin deleted all uploads for '%s' (%d files)", bot_name, count)
    return web.json_response({"status": "ok", "deleted": count})


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/ws/tunnel", handle_ws)
    # Upload + admin routes (before catch-all)
    app.router.add_post("/_upload", handle_upload)
    app.router.add_get("/_admin", handle_admin)
    app.router.add_get("/_admin/uploads/{bot_name}/{filename}", handle_admin_file)
    app.router.add_delete("/_admin/uploads/{bot_name}/{filename}", handle_admin_file)
    app.router.add_get("/_admin/uploads/{bot_name}", handle_admin_bot)
    app.router.add_delete("/_admin/uploads/{bot_name}", handle_admin_delete_bot)
    app.router.add_get("/_admin/uploads", handle_admin)
    app.router.add_route("*", "/{path_info:.*}", handle_http)
    return app


if __name__ == "__main__":
    if not SHARED_SECRET:
        print("ERROR: Set the RELAY_SECRET environment variable before starting.")
        print("  Example: RELAY_SECRET=my-secret-here python relay_server.py")
        raise SystemExit(1)
    log.info("Starting relay on port %d", RELAY_PORT)
    web.run_app(create_app(), host="0.0.0.0", port=RELAY_PORT)
