#!/usr/bin/env python3
"""
Antalogue — decentralised anonymous chat server
Requires: aiohttp, pyyaml
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
import uuid
from pathlib import Path

import yaml
from aiohttp import web

from database import Database

# ── Load config ───────────────────────────────────────────────────────────────

CONFIG_PATH = os.environ.get("ANTALOGUE_CONFIG", "config.yaml")
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

SERVER = CFG.get("server", {})
HOST = SERVER.get("host", "0.0.0.0")
PORT = SERVER.get("port", 8765)
SERVER_NAME = SERVER.get("name", "Antalogue")
MOTD = SERVER.get("motd", "")
IP_SALT = CFG.get("ip_salt", "default-salt")
MAX_MSG_LEN = CFG.get("max_message_length", 5000)
RATE_LIMIT = CFG.get("rate_limit", {}).get("messages_per_minute", 30)
CLEANUP_DAYS = CFG.get("cleanup", {}).get("retention_days", 7)
CLEANUP_HOURS = CFG.get("cleanup", {}).get("interval_hours", 6)

DB_PATH = CFG.get("database", "antalogue.db")
db = Database(DB_PATH)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")
log = logging.getLogger("antalogue")

# ── Ensure default admin exists ───────────────────────────────────────────────

_adm = CFG.get("admin", {})
if _adm:
    db.create_admin(_adm.get("username", "admin"), _adm.get("password", "changeme"), role="admin")

# ── Globals ───────────────────────────────────────────────────────────────────

# Waiting pool: list of dicts {ws, ip_hash, exclude_id: str}
waiting = []

# Active chats: chat_id -> {ws0, ws1, ip0_hash, ip1_hash}
active = {}

# Pending handshakes: chat_id -> {ws0, ws1, ip0_hash, ip1_hash, ip0_raw, ip1_raw, accepted: set}
pending = {}

# ws -> chat_id  (quick lookup)
ws_chat = {}

# ws -> ip_hash
ws_ip = {}

# ws -> raw ip
ws_ip_raw = {}

# Authenticated admin sessions: token -> {username, role}
admin_sessions = {}

# Rate-limit buckets: ws -> [timestamps]
rate_buckets = {}

STATIC_DIR = Path(__file__).parent / "static"


# ── Helpers ───────────────────────────────────────────────────────────────────

def hash_ip(ip: str) -> str:
    return hashlib.sha256(f"{IP_SALT}:{ip}".encode()).hexdigest()


def client_ip(request) -> str:
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    peer = request.transport.get_extra_info("peername")
    return peer[0] if peer else "unknown"


def rate_ok(key) -> bool:
    now = time.time()
    bucket = rate_buckets.setdefault(id(key), [])
    bucket[:] = [t for t in bucket if now - t < 60]
    if len(bucket) >= RATE_LIMIT:
        return False
    bucket.append(now)
    return True


def require_auth(request):
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    session = admin_sessions.get(token)
    if not session:
        raise web.HTTPUnauthorized(text="Not authenticated")
    return session


def require_admin(request):
    session = require_auth(request)
    if session["role"] != "admin":
        raise web.HTTPForbidden(text="Admin access required")
    return session


async def send_json(ws, data):
    try:
        await ws.send_json(data)
    except Exception:
        pass


async def broadcast_online_count(exclude=None):
    count = len(ws_ip)
    msg = {"type": "online_count", "count": count}
    for w in list(ws_ip):
        if w is not exclude:
            await send_json(w, msg)


# ── Matching logic ────────────────────────────────────────────────────────────

def try_match():
    """Try to pair two people in the waiting pool."""
    for i in range(len(waiting)):
        for j in range(i + 1, len(waiting)):
            a, b = waiting[i], waiting[j]
            # Check they haven't just chatted (same exclude ID means they were last partners)
            if a["exclude_id"] and a["exclude_id"] == b["exclude_id"]:
                continue
            # Match found
            waiting.pop(j)
            waiting.pop(i)
            return a, b
    return None


async def do_match():
    result = try_match()
    if not result:
        return
    a, b = result
    chat_id = uuid.uuid4().hex[:16]

    pending[chat_id] = {
        "ws0": a["ws"], "ws1": b["ws"],
        "ip0": a["ip_hash"], "ip1": b["ip_hash"],
        "ip0_raw": ws_ip_raw.get(a["ws"], ""),
        "ip1_raw": ws_ip_raw.get(b["ws"], ""),
        "accepted": set(),
    }
    ws_chat[a["ws"]] = chat_id
    ws_chat[b["ws"]] = chat_id

    log.info("Pending match %s — awaiting handshake", chat_id)

    await send_json(a["ws"], {"type": "match_request", "chat_id": chat_id})
    await send_json(b["ws"], {"type": "match_request", "chat_id": chat_id})


async def confirm_match(chat_id):
    """Both clients accepted — promote pending to active."""
    info = pending.pop(chat_id, None)
    if not info:
        return
    active[chat_id] = {
        "ws0": info["ws0"], "ws1": info["ws1"],
        "ip0": info["ip0"], "ip1": info["ip1"],
    }
    db.add_session(chat_id, info["ip0"], info["ip1"],
                   ip1_raw=info["ip0_raw"], ip2_raw=info["ip1_raw"])
    log.info("Confirmed chat %s", chat_id)
    await send_json(info["ws0"], {"type": "matched", "chat_id": chat_id})
    await send_json(info["ws1"], {"type": "matched", "chat_id": chat_id})


async def abandon_pending(ws, chat_id):
    """Client re-searched or cancelled — drop the pending match, notify partner.
    Partner is NOT put back in waiting (they'll re-search themselves via requeued)."""
    info = pending.pop(chat_id, None)
    if not info:
        return
    ws_chat.pop(info["ws0"], None)
    ws_chat.pop(info["ws1"], None)

    other = info["ws1"] if ws is info["ws0"] else info["ws0"]
    await send_json(other, {"type": "requeued", "chat_id": chat_id})
    log.info("Abandoned pending %s — partner notified", chat_id)


async def reject_match(ws, chat_id):
    """One client rejected via handshake — requeue the other on this server."""
    info = pending.pop(chat_id, None)
    if not info:
        return
    ws_chat.pop(info["ws0"], None)
    ws_chat.pop(info["ws1"], None)

    other = info["ws1"] if ws is info["ws0"] else info["ws0"]
    other_ip = ws_ip.get(other, "")
    waiting.append({"ws": other, "ip_hash": other_ip, "exclude_id": chat_id})
    await send_json(other, {"type": "requeued", "chat_id": chat_id})
    log.info("Rejected pending %s — partner requeued", chat_id)
    await do_match()


def sender_index(ws, chat_id):
    info = active.get(chat_id)
    if not info:
        return -1
    if ws is info["ws0"]:
        return 0
    if ws is info["ws1"]:
        return 1
    return -1


def partner_ws(ws, chat_id):
    info = active.get(chat_id)
    if not info:
        return None
    return info["ws1"] if ws is info["ws0"] else info["ws0"]


async def end_chat(ws, chat_id, notify=True):
    info = active.pop(chat_id, None)
    if not info:
        return
    db.end_session(chat_id)
    ws_chat.pop(info["ws0"], None)
    ws_chat.pop(info["ws1"], None)

    other = info["ws1"] if ws is info["ws0"] else info["ws0"]
    if notify:
        await send_json(other, {"type": "partner_disconnected", "chat_id": chat_id})
    log.info("Ended chat %s", chat_id)


# ── WebSocket handler ─────────────────────────────────────────────────────────

async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    ip = client_ip(request)
    ip_h = hash_ip(ip)
    ws_ip[ws] = ip_h
    ws_ip_raw[ws] = ip

    # Check ban
    ban = db.is_banned(ip_h)
    if ban:
        await send_json(ws, {"type": "error", "message": "You are banned from this server."})
        await ws.close()
        return ws

    # Send server info
    await send_json(ws, {
        "type": "server_info",
        "name": SERVER_NAME,
        "motd": MOTD,
        "online": len(ws_ip),
        "max_message_length": MAX_MSG_LEN,
    })

    log.info("Client connected: %s", ip_h[:12])
    await broadcast_online_count(exclude=ws)

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue

            mtype = data.get("type")

            # ── Search for a partner ──────────────────────────────
            if mtype == "search":
                # Remove from any existing waiting slot
                waiting[:] = [w for w in waiting if w["ws"] is not ws]

                # Clean up any pending handshake this client is in
                old_chat = ws_chat.pop(ws, None)
                if old_chat and old_chat in pending:
                    await abandon_pending(ws, old_chat)

                excludes = data.get("exclude_chat_id", "")
                waiting.append({"ws": ws, "ip_hash": ip_h, "exclude_id": excludes})
                await send_json(ws, {"type": "searching"})
                await do_match()

            # ── Cancel search ─────────────────────────────────────
            elif mtype == "cancel":
                waiting[:] = [w for w in waiting if w["ws"] is not ws]
                old_chat = ws_chat.pop(ws, None)
                if old_chat and old_chat in pending:
                    await abandon_pending(ws, old_chat)
                await send_json(ws, {"type": "cancelled"})

            # ── Send a message ────────────────────────────────────
            elif mtype == "message":
                chat_id = ws_chat.get(ws)
                if not chat_id:
                    continue
                content = str(data.get("content", ""))[:MAX_MSG_LEN]
                if not content.strip():
                    continue
                if not rate_ok(ws):
                    await send_json(ws, {"type": "error", "message": "Slow down."})
                    continue

                idx = sender_index(ws, chat_id)
                db.add_message(chat_id, idx, content)

                other = partner_ws(ws, chat_id)
                if other:
                    await send_json(other, {
                        "type": "message",
                        "content": content,
                        "chat_id": chat_id,
                        "sender": "stranger",
                    })
                # Echo back to sender
                await send_json(ws, {
                    "type": "message",
                    "content": content,
                    "chat_id": chat_id,
                    "sender": "you",
                })

            # ── Typing indicator ──────────────────────────────────
            elif mtype == "typing":
                chat_id = ws_chat.get(ws)
                if chat_id:
                    other = partner_ws(ws, chat_id)
                    if other:
                        await send_json(other, {"type": "typing"})

            # ── Disconnect from chat ──────────────────────────────
            elif mtype == "disconnect":
                chat_id = ws_chat.get(ws)
                if chat_id:
                    # If still pending, treat as reject
                    if chat_id in pending:
                        await reject_match(ws, chat_id)
                    else:
                        await end_chat(ws, chat_id)

            # ── Accept a match (handshake) ────────────────────────
            elif mtype == "accept":
                chat_id = data.get("chat_id", "")
                info = pending.get(chat_id)
                if info and (ws is info["ws0"] or ws is info["ws1"]):
                    info["accepted"].add(id(ws))
                    if len(info["accepted"]) == 2:
                        await confirm_match(chat_id)

            # ── Reject a match (handshake) ────────────────────────
            elif mtype == "reject":
                chat_id = data.get("chat_id", "") or ws_chat.get(ws)
                if chat_id and chat_id in pending:
                    await reject_match(ws, chat_id)

            # ── Report ────────────────────────────────────────────
            elif mtype == "report":
                chat_id = data.get("chat_id", "")
                reason = str(data.get("reason", ""))[:1000]
                if chat_id:
                    db.add_report(chat_id, reason)
                    await send_json(ws, {"type": "report_ack"})

    except Exception as e:
        log.warning("WS error: %s", e)
    finally:
        # Clean up on disconnect
        waiting[:] = [w for w in waiting if w["ws"] is not ws]
        chat_id = ws_chat.pop(ws, None)
        if chat_id:
            if chat_id in pending:
                await reject_match(ws, chat_id)
            elif chat_id in active:
                await end_chat(ws, chat_id)
        ws_ip.pop(ws, None)
        ws_ip_raw.pop(ws, None)
        rate_buckets.pop(id(ws), None)
        log.info("Client disconnected: %s", ip_h[:12])
        await broadcast_online_count()

    return ws


# ── Admin API ─────────────────────────────────────────────────────────────────

async def api_login(request):
    body = await request.json()
    user = db.verify_admin(body.get("username", ""), body.get("password", ""))
    if not user:
        raise web.HTTPUnauthorized(text="Invalid credentials")
    token = secrets.token_hex(32)
    admin_sessions[token] = {"username": user["username"], "role": user["role"]}
    return web.json_response({"token": token, "username": user["username"], "role": user["role"]})


async def api_stats(request):
    require_auth(request)
    stats = db.get_stats()
    stats["waiting"] = len(waiting)
    stats["connected"] = len(ws_ip)
    return web.json_response(stats)


async def api_reports(request):
    require_auth(request)
    status = request.query.get("status")
    reports = db.get_reports(status=status)
    return web.json_response(reports)


async def api_resolve_report(request):
    require_auth(request)
    rid = int(request.match_info["id"])
    body = await request.json()
    db.resolve_report(rid, body.get("status", "resolved"))
    return web.json_response({"ok": True})


async def api_messages(request):
    require_auth(request)
    chat_id = request.match_info["chat_id"]
    msgs = db.get_messages(chat_id)
    return web.json_response(msgs)


async def api_sessions(request):
    session = require_auth(request)
    sessions = db.get_sessions(limit=200)
    if session["role"] != "admin":
        for s in sessions:
            s.pop("ip1_raw", None)
            s.pop("ip2_raw", None)
    return web.json_response(sessions)


async def api_bans(request):
    session = require_auth(request)
    bans = db.list_bans()
    if session["role"] != "admin":
        for b in bans:
            b.pop("ip_raw", None)
    return web.json_response(bans)


async def api_add_ban(request):
    require_auth(request)
    body = await request.json()
    ip_hash = body.get("ip_hash", "")
    ip_raw = body.get("ip_raw", "")
    # Admin submits a raw IP — hash it server-side
    if body.get("ip_raw_input"):
        ip_raw = body["ip_raw_input"]
        ip_hash = hash_ip(ip_raw)
    # Allow banning by chat_id + sender_index
    if not ip_hash and body.get("chat_id") is not None:
        info = db.get_ip_for_chat(body["chat_id"], body.get("sender_index", 0))
        if info:
            ip_hash = info["ip_hash"]
            ip_raw = info.get("ip_raw", "")
    if not ip_hash:
        raise web.HTTPBadRequest(text="No ip_hash, ip_raw_input, or chat_id provided")
    session = require_auth(request)
    db.ban_ip(
        ip_hash,
        reason=body.get("reason", ""),
        banned_by=session["username"],
        duration_hours=body.get("duration_hours"),
        ip_raw=ip_raw,
    )
    # Kick currently connected clients with that IP
    to_close = [ws for ws, iph in ws_ip.items() if iph == ip_hash]
    for ws in to_close:
        await send_json(ws, {"type": "error", "message": "You have been banned."})
        await ws.close()
    return web.json_response({"ok": True})


async def api_remove_ban(request):
    require_auth(request)
    bid = int(request.match_info["id"])
    db.remove_ban(bid)
    return web.json_response({"ok": True})


async def api_admins(request):
    require_admin(request)
    return web.json_response(db.list_admins())


async def api_create_admin(request):
    require_admin(request)
    body = await request.json()
    ok = db.create_admin(body["username"], body["password"], body.get("role", "moderator"))
    if not ok:
        raise web.HTTPConflict(text="Username already exists")
    return web.json_response({"ok": True})


async def api_delete_admin(request):
    require_admin(request)
    aid = int(request.match_info["id"])
    db.delete_admin(aid)
    return web.json_response({"ok": True})


async def api_search_messages(request):
    require_auth(request)
    q = request.query.get("q", "")
    if len(q) < 2:
        raise web.HTTPBadRequest(text="Query too short")
    return web.json_response(db.search_messages(q))


# ── Static file serving ──────────────────────────────────────────────────────

async def serve_index(request):
    return web.FileResponse(STATIC_DIR / "index.html")


async def serve_admin(request):
    return web.FileResponse(STATIC_DIR / "admin.html")


# ── Periodic cleanup ─────────────────────────────────────────────────────────

async def cleanup_task():
    while True:
        await asyncio.sleep(CLEANUP_HOURS * 3600)
        deleted = db.cleanup(CLEANUP_DAYS)
        log.info("Cleanup: removed %d old records", deleted)


# ── CORS middleware ───────────────────────────────────────────────────────────

@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as ex:
            resp = ex
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp


# ── App setup ─────────────────────────────────────────────────────────────────

def create_app():
    app = web.Application(middlewares=[cors_middleware])

    # WebSocket
    app.router.add_get("/ws", ws_handler)

    # Admin API
    app.router.add_post("/api/login", api_login)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/reports", api_reports)
    app.router.add_post("/api/reports/{id}/resolve", api_resolve_report)
    app.router.add_get("/api/messages/{chat_id}", api_messages)
    app.router.add_get("/api/sessions", api_sessions)
    app.router.add_get("/api/bans", api_bans)
    app.router.add_post("/api/bans", api_add_ban)
    app.router.add_delete("/api/bans/{id}", api_remove_ban)
    app.router.add_get("/api/admins", api_admins)
    app.router.add_post("/api/admins", api_create_admin)
    app.router.add_delete("/api/admins/{id}", api_delete_admin)
    app.router.add_get("/api/messages", api_search_messages)

    # Static pages
    app.router.add_get("/", serve_index)
    app.router.add_get("/admin", serve_admin)

    # Background cleanup task
    async def start_cleanup(app):
        app["cleanup_task"] = asyncio.ensure_future(cleanup_task())

    async def stop_cleanup(app):
        app["cleanup_task"].cancel()
        try:
            await app["cleanup_task"]
        except asyncio.CancelledError:
            pass

    app.on_startup.append(start_cleanup)
    app.on_cleanup.append(stop_cleanup)

    return app


if __name__ == "__main__":
    log.info("Starting %s on %s:%d", SERVER_NAME, HOST, PORT)
    web.run_app(create_app(), host=HOST, port=PORT)