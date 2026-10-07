"""Relay — team incident + status hub on IKAREM. Watches services, opens
incidents, pages responders, proves the framework while doing it.

Every subsystem earns its place: sessions + CSRF (browser), JWT roles +
scopes (API), API keys (ingesters), SpikeManager (flood lane), nitro
(status rollups), Room (live feed), durable queue (notifications), cron
(probes), MCP tools, NISH mode. /debug/ikarem renders the framework's
own pulse: SpikeManager snapshot, nitro stats, route + tool counts.
"""

import asyncio
import hashlib
import secrets
import time
import urllib.request
import uuid
from pathlib import Path

from ikarem import (
    APIKeyAuth,
    BackgroundTasks,
    CSRFMiddleware,
    Depends,
    Field,
    Ikarem,
    NotFound,
    RateLimitMiddleware,
    RequestIDMiddleware,
    Schema,
    SecurityHeadersMiddleware,
    SessionMiddleware,
    SpikeManager,
    WebSocketDisconnect,
    abort,
    check_password,
    create_token,
    csrf_token,
    flash,
    get_flashed_messages,
    hash_password,
    nitro,
    require_roles,
)
from ikarem.db import DatabasePlugin
from ikarem.http import HTMLResponse, RedirectResponse, escape_html
from ikarem.queue import QueuePlugin, task
from ikarem.websocket import Room

BASE = Path(__file__).parent

spike = SpikeManager(
    initial=50,
    min_limit=5,
    max_limit=200,
    target_latency=0.2,
    queue_timeout=0.3,
    exempt_paths=("/healthz", "/readyz"),
)
feed = Room()

app = Ikarem(
    auth_secret="relay-dev-auth-change-in-prod",
    session_secret="relay-dev-session-change-in-prod",
    db_url="sqlite:///relay.db",
)
app.use(RequestIDMiddleware())
app.use(SecurityHeadersMiddleware())
app.use(SessionMiddleware())
app.use(CSRFMiddleware(exempt_paths=["/api/*", "/mcp"]))
app.use(RateLimitMiddleware(per_minute=240))
app.use(spike)
app.register(DatabasePlugin(app.config.get("db_url", "sqlite:///relay.db")))
app.register(QueuePlugin())
app.mount_static("/static", str(BASE / "static"))
app.mount_mcp("/mcp")
app.nish_mode()


# ---- schema ----


def _ddl(dialect: str, table: str, cols: str) -> str:
    serial = "SERIAL PRIMARY KEY" if dialect == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    return f"CREATE TABLE IF NOT EXISTS {table} (id {serial}, {cols})"


@app.on_startup
async def init_db():
    db = app.state_db
    dialect = getattr(db, "dialect", "sqlite")
    await db.execute(
        "CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE, pw TEXT, role TEXT)"
    )
    await db.execute(_ddl(dialect, "services", "name TEXT, url TEXT, owner_id TEXT, created REAL"))
    await db.execute(
        _ddl(
            dialect,
            "incidents",
            "service_id INTEGER, title TEXT, status TEXT, opened TEXT, updated REAL, opened_by TEXT",
        )
    )
    await db.execute(
        _ddl(dialect, "probe_results", "service_id INTEGER, ts REAL, ok INTEGER, ms INTEGER, code INTEGER")
    )
    await db.execute(_ddl(dialect, "api_keys", "name TEXT, key_hash TEXT, owner_id TEXT, created REAL"))
    await db.execute(_ddl(dialect, "notifications", "kind TEXT, ref INTEGER, created REAL"))
    if str(app.config.get("demo", "true")).lower() in ("1", "true", "yes", "on"):
        if await db.fetch_one("SELECT id FROM users LIMIT 1") is None:
            await _seed_demo(db)


async def _seed_demo(db):
    admin_pw = hash_password("admin1234")
    op_pw = hash_password("op1234")
    await db.execute("INSERT INTO users (email, pw, role) VALUES (?, ?, ?)", "admin@ex.co", admin_pw, "admin")
    await db.execute("INSERT INTO users (email, pw, role) VALUES (?, ?, ?)", "op@ex.co", op_pw, "responder")
    admin = await db.fetch_one("SELECT id FROM users WHERE email = ?", "admin@ex.co")
    for name, url in [
        ("docs site", "https://ikarem.vercel.app/"),
        ("example", "https://example.com/"),
        ("ledger demo", "http://127.0.0.1:8000/healthz"),
    ]:
        await db.execute(
            "INSERT INTO services (name, url, owner_id, created) VALUES (?, ?, ?, ?)",
            name,
            url,
            admin["id"],
            time.time(),
        )


# ---- helpers ----


async def _me(req):
    uid = getattr(req, "session", {}).get("uid")
    if not uid:
        return None
    return await app.state_db.fetch_one("SELECT id, email, role FROM users WHERE id = ?", uid)


def _wants_json(req):
    return req.headers.get("content-type", "").split(";")[0].strip().lower() == "application/json"


async def _fields(req):
    """HTML forms and JSON clients share every mutating endpoint."""
    ctype = req.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype == "application/json":
        try:
            body = await req.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}
    try:
        form = await req.form()
        return {k: form.get(k, "") for k in form.keys()}
    except Exception:
        return {}


def _need_login_link():
    return '<p class="callout">You need to <a href="/login">log in</a> first.</p>'


def layout(title, body, active="", user=None, req=None):
    email = escape_html(user["email"]) if user else ""
    role = escape_html(user["role"]) if user else ""
    nav = "".join(
        f'<a href="{href}" class="{"on" if active == key else ""}">{label}</a>'
        for key, href, label in [
            ("dash", "/", "Overview"),
            ("services", "/services", "Services"),
            ("incidents", "/incidents", "Incidents"),
            ("settings", "/settings", "Keys"),
            ("debug", "/debug/ikarem", "IKAREM"),
        ]
    )
    who = (
        f'<span class="who">{email} · {role}</span>'
        f'<form method="post" action="/logout" class="inline">'
        f'<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">'
        '<button class="btn small">Out</button></form>'
        if user
        else '<a class="btn small" href="/login">Log in</a>'
    )
    flashes = "".join(
        f'<p class="flash">{escape_html(m)}</p>' for m in get_flashed_messages(req) if req is not None
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape_html(title)} — Relay</title>"
        "<link rel='stylesheet' href='/static/style.css'></head><body>"
        f"<header class='top'><a class='brand' href='/'>RELAY</a><nav>{nav}</nav>"
        f"<span class='sp'></span>{who}</header>"
        f"<main>{flashes}{body}</main>"
        "<footer>Relay on IKAREM — status hub. Zero-dep core.</footer></body></html>"
    )


def page(title, body, active="", user=None, req=None):
    return HTMLResponse(layout(title, body, active, user, req))


def _chip(status):
    dot = {"open": "bad", "down": "bad", "acked": "warn", "resolved": "ok", "up": "ok"}.get(status, "mut")
    return f"<span class='dot {dot}'></span>{escape_html(status)}"


async def _insert_id(db, sql, *args):
    """INSERT returning the row id on both dialects (ledger convention)."""
    if getattr(db, "dialect", "sqlite") == "postgres":
        row = await db.fetch_one(sql + " RETURNING id", *args)
        return int(row["id"])
    cur = await db.execute(sql, *args)
    try:
        return int(cur)
    except (TypeError, ValueError):
        return int(cur.lastrowid)


# ---- nitro rollups (module-level so tests can clear them) ----


@nitro(ttl=30.0, maxsize=4)
async def status_rollup():
    db = app.state_db
    services = await db.fetch_all("SELECT id FROM services")
    latest = await db.fetch_all(
        "SELECT service_id, ok FROM probe_results WHERE id IN "
        "(SELECT MAX(id) FROM probe_results GROUP BY service_id)"
    )
    seen = {r["service_id"]: r["ok"] for r in latest}
    up = sum(1 for s in services if seen.get(s["id"]) == 1)
    down = sum(1 for s in services if s["id"] in seen and seen[s["id"]] != 1)
    open_n = await db.fetch_one("SELECT COUNT(*) AS n FROM incidents WHERE status != 'resolved'")
    acked = await db.fetch_one("SELECT COUNT(*) AS n FROM incidents WHERE status = 'acked'")
    return {
        "services": len(services),
        "up": up,
        "down": down,
        "unknown": len(services) - up - down,
        "open_incidents": (open_n["n"] if open_n else 0),
        "acked": (acked["n"] if acked else 0),
    }


@nitro(ttl=30.0, maxsize=4)
async def service_list():
    db = app.state_db
    rows = await db.fetch_all("SELECT id, name, url FROM services ORDER BY id")
    latest = await db.fetch_all(
        "SELECT service_id, ok, ms, code, ts FROM probe_results WHERE id IN "
        "(SELECT MAX(id) FROM probe_results GROUP BY service_id)"
    )
    seen = {r["service_id"]: dict(r) for r in latest}
    out = []
    for s in rows:
        last = seen.get(s["id"], {})
        out.append(
            {
                "id": s["id"],
                "name": s["name"],
                "url": s["url"],
                "state": "up" if last.get("ok") == 1 else ("down" if last else "unknown"),
                "ms": last.get("ms"),
                "code": last.get("code"),
            }
        )
    return out


# ---- queue + probes ----


@task("notify")
async def notify(kind="", ref=0):
    db = getattr(app, "state_db", None)
    if db is None:  # worker without app state: nothing to record into
        return
    await db.execute(
        "INSERT INTO notifications (kind, ref, created) VALUES (?, ?, ?)", kind, ref, time.time()
    )


async def _open_incident(db, service_id, title, opened_by):
    row = await db.fetch_one(
        "SELECT id FROM incidents WHERE service_id = ? AND status != 'resolved' ORDER BY id DESC LIMIT 1",
        service_id,
    )
    if row:
        return row["id"], False
    rid = await _insert_id(
        db,
        "INSERT INTO incidents (service_id, title, status, opened, updated, opened_by)"
        " VALUES (?, ?, 'open', ?, ?, ?)",
        service_id,
        title,
        time.strftime("%Y-%m-%d %H:%M"),
        time.time(),
        opened_by,
    )
    try:
        await feed.broadcast({"event": "incident.opened", "id": int(rid), "title": title})
    except Exception:
        pass
    try:
        queue = getattr(app, "state_queue", None)
        if queue is not None:
            await queue.enqueue("notify", {"kind": "incident.opened", "ref": int(rid)})
    except Exception:
        pass
    for fn in (status_rollup, service_list):
        try:
            fn.cache_clear()
        except Exception:
            pass
    return int(rid), True


async def probe_service(svc):
    url = svc["url"]
    t0 = time.monotonic()

    def _hit():
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return True, int(r.status), int((time.monotonic() - t0) * 1000)
        except Exception:
            return False, 0, int((time.monotonic() - t0) * 1000)

    ok, code, ms = await asyncio.to_thread(_hit)
    return ok, ms, code


async def _record_result(db, service_id, ok, ms, code, opened_by="probe"):
    """Record one probe; returns True when it newly opened an incident."""
    await db.execute(
        "INSERT INTO probe_results (service_id, ts, ok, ms, code) VALUES (?, ?, ?, ?, ?)",
        service_id,
        time.time(),
        1 if ok else 0,
        ms,
        code,
    )
    await db.execute(
        "DELETE FROM probe_results WHERE service_id = ? AND id NOT IN "
        "(SELECT id FROM probe_results WHERE service_id = ? ORDER BY id DESC LIMIT 200)",
        service_id,
        service_id,
    )
    created = False
    if not ok:
        svc = await db.fetch_one("SELECT name FROM services WHERE id = ?", service_id)
        name = svc["name"] if svc else f"service {service_id}"
        _, created = await _open_incident(
            db, service_id, f"{name} probing {code or 'unreachable'}", opened_by
        )
    for fn in (status_rollup, service_list):
        try:
            fn.cache_clear()
        except Exception:
            pass
    return created


async def probe_all(app_ref=None):
    db = (app_ref or app).state_db
    for svc in await db.fetch_all("SELECT id, url FROM services ORDER BY id"):
        ok, ms, code = await probe_service(svc)
        await _record_result(db, svc["id"], ok, ms, code)


@app.every(120)
async def probe_tick():
    await probe_all(app)


# ---- pages ----


@app.get("/")
async def dashboard(req):
    user = await _me(req)
    roll = await status_rollup()
    db = app.state_db
    recent = await db.fetch_all(
        "SELECT i.id, i.title, i.status, i.opened, s.name FROM incidents i "
        "LEFT JOIN services s ON s.id = i.service_id ORDER BY i.id DESC LIMIT 10"
    )
    rows = (
        "".join(
            f"<tr><td>#{r['id']}</td><td><a href='/incidents/{r['id']}'>{escape_html(r['title'])}</a></td>"
            f"<td>{escape_html(r['name'] or '?')}</td><td>{_chip(r['status'])}</td>"
            f"<td class='mut'>{escape_html(r['opened'])}</td></tr>"
            for r in recent
        )
        or "<tr><td colspan='5' class='mut'>No incidents. Quiet is good.</td></tr>"
    )
    body = (
        "<h1>Status</h1>"
        f"<div class='cards'><div class='card'><b>{roll['up']}</b><span>up</span></div>"
        f"<div class='card bad'><b>{roll['down']}</b><span>down</span></div>"
        f"<div class='card'><b>{roll['unknown']}</b><span>unknown</span></div>"
        f"<div class='card warn'><b>{roll['open_incidents']}</b><span>open</span></div></div>"
        "<h2>Latest incidents</h2><table><tr><th>#</th><th>Title</th><th>Service</th>"
        "<th>Status</th><th>Opened</th></tr>" + rows + "</table>"
        "<p class='mut'>Live feed: <code>/ws/feed</code> · machine view: "
        "<a href='/api/status'>/api/status</a> · <a href='/debug/ikarem'>framework pulse</a></p>"
    )
    return page("Status", body, "dash", user, req)


@app.get("/services")
async def services_page(req):
    user = await _me(req)
    items = (
        "".join(
            f"<tr><td><a href='/services/{s['id']}'>{escape_html(s['name'])}</a></td>"
            f"<td class='mut'>{escape_html(s['url'])}</td>"
            f"<td>{_chip(s['state'])}</td>"
            f"<td class='mut'>{s['ms'] if s['ms'] is not None else '—'} ms</td></tr>"
            for s in await service_list()
        )
        or "<tr><td colspan='4' class='mut'>No services yet.</td></tr>"
    )
    form = "<h2>Add a service</h2>" + (
        f"<form method='post' action='/services' class='form'>"
        f"<input type='hidden' name='_csrf_token' value='{csrf_token(req)}'>"
        "<input name='name' placeholder='name' maxlength='80' required> "
        "<input name='url' placeholder='https://…' maxlength='500' required> "
        "<button>Add</button></form>"
        if user
        else _need_login_link()
    )
    body = (
        "<h1>Services</h1><table><tr><th>Name</th><th>URL</th><th>State</th><th>Probe</th></tr>"
        + items
        + "</table>"
        + form
    )
    return page("Services", body, "services", user, req)


@app.get("/services/{sid:int}")
async def service_detail(req, sid: int):
    user = await _me(req)
    db = app.state_db
    svc = await db.fetch_one("SELECT id, name, url FROM services WHERE id = ?", sid)
    if not svc:
        raise NotFound("No such service")
    probes = await db.fetch_all(
        "SELECT ts, ok, ms, code FROM probe_results WHERE service_id = ? ORDER BY id DESC LIMIT 20", sid
    )
    bars = "".join(
        f"<span class='bar {'ok' if p['ok'] == 1 else 'bad'}' title='{p['code']} · {p['ms']}ms'></span>"
        for p in reversed(probes)
    )
    incs = await db.fetch_all(
        "SELECT id, title, status FROM incidents WHERE service_id = ? ORDER BY id DESC LIMIT 10", sid
    )
    inc_rows = "".join(
        f"<tr><td>#{i['id']}</td><td><a href='/incidents/{i['id']}'>{escape_html(i['title'])}</a></td>"
        f"<td>{_chip(i['status'])}</td></tr>"
        for i in incs
    )
    probe_btn = (
        f"<form method='post' action='/services/{sid}/probe' class='inline'>"
        f"<input type='hidden' name='_csrf_token' value='{csrf_token(req)}'>"
        "<button class='btn small'>Probe now</button></form>"
        if user
        else ""
    )
    body = (
        f"<h1>{escape_html(svc['name'])} {probe_btn}</h1>"
        f"<p class='mut'>{escape_html(svc['url'])}</p>"
        f"<h2>Last {len(probes)} probes</h2><div class='bars'>{bars or '<span class=mut>none yet</span>'}</div>"
        "<h2>Incidents</h2><table><tr><th>#</th><th>Title</th><th>Status</th></tr>"
        + (inc_rows or "<tr><td colspan='3' class='mut'>None.</td></tr>")
        + "</table>"
    )
    return page(svc["name"], body, "services", user, req)


@app.post("/services")
async def service_create(req):
    user = await _me(req)
    if not user:
        abort(401, "log in first")
    form = await _fields(req)
    name = (form.get("name", "") or "").strip()[:80]
    url = (form.get("url", "") or "").strip()[:500]
    if not name or not (url.startswith("http://") or url.startswith("https://")):
        return page(
            "Services",
            "<p class='callout bad'>Name required; URL must start with http(s)://.</p>",
            "services",
            user,
            req,
        )
    db = app.state_db
    rid = await _insert_id(
        db,
        "INSERT INTO services (name, url, owner_id, created) VALUES (?, ?, ?, ?)",
        name,
        url,
        user["id"],
        time.time(),
    )
    for fn in (status_rollup, service_list):
        fn.cache_clear()
    flash(req, f"Service {name} added.")
    return RedirectResponse(f"/services/{rid}", 303)


@app.post("/services/{sid:int}/probe")
async def service_probe_now(req, sid: int):
    user = await _me(req)
    if not user:
        abort(401, "log in first")
    db = app.state_db
    svc = await db.fetch_one("SELECT id, url FROM services WHERE id = ?", sid)
    if not svc:
        raise NotFound("No such service")
    ok, ms, code = await probe_service(svc)
    await _record_result(db, sid, ok, ms, code, opened_by=user["email"])
    flash(req, f"Probed: {'up' if ok else 'down'} ({code}, {ms} ms).")
    return RedirectResponse(f"/services/{sid}", 303)


@app.get("/incidents")
async def incidents_page(req):
    user = await _me(req)
    filt = req.query.get("status", "")
    db = app.state_db
    if filt in ("open", "acked", "resolved"):
        rows = await db.fetch_all(
            "SELECT i.id, i.title, i.status, i.opened, s.name FROM incidents i "
            "LEFT JOIN services s ON s.id = i.service_id WHERE i.status = ? ORDER BY i.id DESC LIMIT 50",
            filt,
        )
    else:
        rows = await db.fetch_all(
            "SELECT i.id, i.title, i.status, i.opened, s.name FROM incidents i "
            "LEFT JOIN services s ON s.id = i.service_id ORDER BY i.id DESC LIMIT 50"
        )
    items = (
        "".join(
            f"<tr><td>#{r['id']}</td><td><a href='/incidents/{r['id']}'>{escape_html(r['title'])}</a></td>"
            f"<td>{escape_html(r['name'] or '?')}</td><td>{_chip(r['status'])}</td>"
            f"<td class='mut'>{escape_html(r['opened'])}</td></tr>"
            for r in rows
        )
        or "<tr><td colspan='5' class='mut'>None. Quiet is good.</td></tr>"
    )
    links = " · ".join(
        f"<a href='/incidents{('?status=' + s) if s else ''}'>{s or 'all'}</a>"
        for s in ("", "open", "acked", "resolved")
    )
    form = "<h2>Open one</h2>" + (
        f"<form method='post' action='/incidents' class='form'>"
        f"<input type='hidden' name='_csrf_token' value='{csrf_token(req)}'>"
        "<input name='title' placeholder='what broke' maxlength='140' required> "
        "<input name='service_id' placeholder='service #' inputmode='numeric'> "
        "<button>Open</button></form>"
        if user
        else _need_login_link()
    )
    body = f"<h1>Incidents</h1><p class='mut'>{links}</p><table><tr><th>#</th><th>Title</th><th>Service</th><th>Status</th><th>Opened</th></tr>{items}</table>{form}"
    return page("Incidents", body, "incidents", user, req)


@app.get("/incidents/{iid:int}")
async def incident_detail(req, iid: int):
    user = await _me(req)
    db = app.state_db
    inc = await db.fetch_one(
        "SELECT i.id, i.title, i.status, i.opened, i.opened_by, s.name FROM incidents i "
        "LEFT JOIN services s ON s.id = i.service_id WHERE i.id = ?",
        iid,
    )
    if not inc:
        raise NotFound("No such incident")
    acts = ""
    if user and inc["status"] != "resolved":
        acts = (
            f"<form method='post' action='/incidents/{iid}/ack' class='inline'>"
            f"<input type='hidden' name='_csrf_token' value='{csrf_token(req)}'>"
            "<button class='btn small'>Ack</button></form> "
            f"<form method='post' action='/incidents/{iid}/resolve' class='inline'>"
            f"<input type='hidden' name='_csrf_token' value='{csrf_token(req)}'>"
            "<button class='btn small'>Resolve</button></form>"
        )
    body = (
        f"<h1>#{inc['id']} {escape_html(inc['title'])}</h1>"
        f"<p>{_chip(inc['status'])} <span class='mut'>· {escape_html(inc['name'] or '?')} · "
        f"opened {escape_html(inc['opened'])} by {escape_html(inc['opened_by'])}</span></p>"
        f"<p>{acts}</p>"
    )
    return page(f"Incident {iid}", body, "incidents", user, req)


@app.post("/incidents")
async def incident_create(req):
    user = await _me(req)
    if not user:
        abort(401, "log in first")
    form = await _fields(req)
    title = (form.get("title", "") or "").strip()[:140]
    try:
        service_id = int(form.get("service_id", "") or 0)
    except ValueError:
        service_id = 0
    if not title:
        return page("Incidents", "<p class='callout bad'>Title required.</p>", "incidents", user, req)
    db = app.state_db
    if service_id:
        svc = await db.fetch_one("SELECT id FROM services WHERE id = ?", service_id)
        if not svc:
            return page(
                "Incidents",
                "<p class='callout bad'>No such service.</p>",
                "incidents",
                user,
                req,
            )
    else:
        first = await db.fetch_one("SELECT id FROM services ORDER BY id LIMIT 1")
        service_id = first["id"] if first else 0
    iid, _ = await _open_incident(db, service_id, title, user["email"])
    flash(req, f"Incident #{iid} opened.")
    return RedirectResponse(f"/incidents/{iid}", 303)


async def _transition(req, iid, to):
    user = await _me(req)
    if not user:
        abort(401, "log in first")
    db = app.state_db
    inc = await db.fetch_one("SELECT id, title, status FROM incidents WHERE id = ?", iid)
    if not inc:
        raise NotFound("No such incident")
    await db.execute("UPDATE incidents SET status = ?, updated = ? WHERE id = ?", to, time.time(), iid)
    try:
        await feed.broadcast({"event": f"incident.{to}", "id": iid, "title": inc["title"]})
    except Exception:
        pass
    flash(req, f"Incident #{iid} {to}.")
    return RedirectResponse(f"/incidents/{iid}", 303)


@app.post("/incidents/{iid:int}/ack")
async def incident_ack(req, iid: int):
    return await _transition(req, iid, "acked")


@app.post("/incidents/{iid:int}/resolve")
async def incident_resolve(req, iid: int):
    return await _transition(req, iid, "resolved")


# ---- auth pages ----


@app.get("/register")
async def register_page(req):
    body = (
        "<h1>Register</h1><form method='post' action='/register' class='form'>"
        f"<input type='hidden' name='_csrf_token' value='{csrf_token(req)}'>"
        "<input name='email' placeholder='email' maxlength='254' required> "
        "<input name='password' type='password' placeholder='password (8+ chars)' required> "
        "<button>Create</button></form>"
    )
    return page("Register", body, "", await _me(req), req)


@app.post("/register")
async def register(req):
    form = await _fields(req)
    email = (form.get("email", "") or "").strip().lower()[:254]
    pw = form.get("password", "") or ""
    if "@" not in email or len(pw) < 8:
        return page("Register", "<p class='callout bad'>Valid email + 8-char password.</p>", "", None, req)
    db = app.state_db
    if await db.fetch_one("SELECT id FROM users WHERE email = ?", email):
        return page("Register", "<p class='callout bad'>That email is taken.</p>", "", None, req)
    uid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO users (id, email, pw, role) VALUES (?, ?, ?, 'viewer')",
        uid,
        email,
        hash_password(pw),
    )
    req.session["uid"] = uid
    if _wants_json(req):
        return {"id": uid, "email": email}, 201
    flash(req, "Welcome aboard.")
    return RedirectResponse("/", 303)


@app.get("/login")
async def login_page(req):
    body = (
        "<h1>Log in</h1><form method='post' action='/login' class='form'>"
        f"<input type='hidden' name='_csrf_token' value='{csrf_token(req)}'>"
        "<input name='email' placeholder='email' required> "
        "<input name='password' type='password' placeholder='password' required> "
        "<button>Log in</button></form>"
        "<p class='mut'>Demo: admin@ex.co / admin1234 (IKAREM_DEMO=true seeds it).</p>"
    )
    return page("Log in", body, "", await _me(req), req)


@app.post("/login")
async def login(req):
    form = await _fields(req)
    email = (form.get("email", "") or "").strip().lower()
    row = await app.state_db.fetch_one("SELECT id, pw FROM users WHERE email = ?", email)
    if not row or not check_password(form.get("password", "") or "", row["pw"]):
        if _wants_json(req):
            abort(401, "wrong email or password")
        return page("Log in", "<p class='callout bad'>Wrong email or password.</p>", "", None, req)
    req.session["uid"] = row["id"]
    if _wants_json(req):
        return {"ok": True, "email": email}
    flash(req, "Back on shift.")
    return RedirectResponse("/", 303)


@app.post("/logout")
async def logout(req):
    req.session.clear()
    return RedirectResponse("/login", 303)


# ---- settings: API keys ----


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def _mint_key(db, owner_id, name):
    raw = "rl_" + secrets.token_urlsafe(32)
    rid = await _insert_id(
        db,
        "INSERT INTO api_keys (name, key_hash, owner_id, created) VALUES (?, ?, ?, ?)",
        name,
        _hash_key(raw),
        owner_id,
        time.time(),
    )
    return rid, raw


async def _key_lookup(key: str):
    if not key.startswith("rl_"):
        return None
    row = await app.state_db.fetch_one(
        "SELECT ak.name, u.email FROM api_keys ak JOIN users u ON u.id = ak.owner_id WHERE ak.key_hash = ?",
        _hash_key(key),
    )
    if not row:
        return None
    return {"sub": row["email"], "key_name": row["name"]}


@app.get("/settings")
async def settings_page(req):
    user = await _me(req)
    if not user:
        return page("Keys", _need_login_link(), "settings", None, req)
    db = app.state_db
    keys = await db.fetch_all(
        "SELECT id, name, created FROM api_keys WHERE owner_id = ? ORDER BY id", user["id"]
    )
    rows = (
        "".join(
            f"<tr><td>{escape_html(k['name'])}</td>"
            f"<td class='mut'>{time.strftime('%Y-%m-%d', time.localtime(k['created']))}</td>"
            f"<td><form method='post' action='/settings/keys/{k['id']}/revoke' class='inline'>"
            f"<input type='hidden' name='_csrf_token' value='{csrf_token(req)}'>"
            "<button class='btn small danger'>Revoke</button></form></td></tr>"
            for k in keys
        )
        or "<tr><td colspan='3' class='mut'>No keys. Mint one for ingesters.</td></tr>"
    )
    body = (
        "<h1>API keys</h1><p class='mut'>Ingesters use <code>X-API-Key</code> on "
        "<code>POST /api/ingest</code>. Secrets show once, hashed at rest.</p>"
        "<table><tr><th>Name</th><th>Created</th><th></th></tr>" + rows + "</table>"
        "<h2>Mint</h2><form method='post' action='/settings/keys' class='form'>"
        f"<input type='hidden' name='_csrf_token' value='{csrf_token(req)}'>"
        "<input name='name' placeholder='ingester name' maxlength='60' required> "
        "<button>Mint key</button></form>"
    )
    return page("Keys", body, "settings", user, req)


@app.post("/settings/keys")
async def key_mint(req):
    user = await _me(req)
    if not user:
        abort(401, "log in first")
    form = await _fields(req)
    name = (form.get("name", "") or "").strip()[:60] or "ingester"
    _, raw = await _mint_key(app.state_db, user["id"], name)
    flash(req, f"Key minted (shows once): {raw}")
    return RedirectResponse("/settings", 303)


@app.post("/settings/keys/{kid:int}/revoke")
async def key_revoke(req, kid: int):
    user = await _me(req)
    if not user:
        abort(401, "log in first")
    await app.state_db.execute("DELETE FROM api_keys WHERE id = ? AND owner_id = ?", kid, user["id"])
    flash(req, "Key revoked.")
    return RedirectResponse("/settings", 303)


# ---- debug: the framework's own pulse ----


@app.get("/debug/ikarem")
async def debug_ikarem(req):
    user = await _me(req)
    if not user:
        return page("IKAREM", _need_login_link(), "debug", None, req)
    snap = spike.snapshot()
    tools = app.mcp_tools()
    rep = app.check()
    roll_info = status_rollup.cache_info()
    list_info = service_list.cache_info()

    def kv(d):
        return "".join(f"<tr><td>{escape_html(k)}</td><td>{escape_html(v)}</td></tr>" for k, v in d.items())

    body = (
        "<h1>Framework pulse</h1><p class='mut'>Relay watches itself with the same "
        "primitives it sells: this page reads them live.</p>"
        "<h2>SpikeManager</h2><table>"
        + kv({k: snap[k] for k in ("limit", "in_flight", "admitted", "shed", "queued")})
        + f"<tr><td>avg_latency</td><td>{snap['avg_latency']}</td></tr></table>"
        "<h2>Nitro</h2><table><tr><th>fn</th><th>hits</th><th>misses</th>"
        "<th>coalesced</th><th>size</th></tr>"
        + "".join(
            f"<tr><td>{name}</td><td>{i['hits']}</td><td>{i['misses']}</td>"
            f"<td>{i['coalesced']}</td><td>{i['size']}</td></tr>"
            for name, i in (("status_rollup", roll_info), ("service_list", list_info))
        )
        + "</table>"
        f"<h2>Audit</h2><p>Routes: <b>{len(rep['routes'])}</b> · MCP tools: "
        f"<b>{len(tools)}</b> · check errors: <b>{len(rep['errors'])}</b> · "
        f"warnings: <b>{len(rep['warnings'])}</b> (public writes are acknowledged, not hidden).</p>"
    )
    return page("IKAREM", body, "debug", user, req)


# ---- JSON API ----


class ServiceIn(Schema):
    name: str = Field(..., min_length=1, max_length=80)
    url: str = Field(..., min_length=8, max_length=500)


class IncidentIn(Schema):
    service_id: int = 0
    title: str = Field(..., min_length=1, max_length=140)


class LoginIn(Schema):
    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=1, max_length=256)


@app.get("/api/csrf")
async def api_csrf(req):
    return {"csrf": csrf_token(req)}


@app.post("/api/login")
async def api_login(req, body: LoginIn):
    row = await app.state_db.fetch_one(
        "SELECT id, pw, role, email FROM users WHERE email = ?", body.email.strip().lower()
    )
    if not row or not check_password(body.password, row["pw"]):
        abort(401, "wrong email or password")
    token = create_token(row["id"], app.config.get("auth_secret"), expires_in=3600, roles=[row["role"]])
    return {"token": token, "email": row["email"], "role": row["role"]}


@app.get("/api/status")
async def api_status(req):
    return await status_rollup()


@app.get("/api/services")
async def api_services(req):
    return {"services": await service_list()}


@app.post("/api/services")
async def api_service_create(req, body: ServiceIn, claims=Depends(require_roles("responder", "admin"))):
    if not (body.url.startswith("http://") or body.url.startswith("https://")):
        abort(400, "url must start with http(s)://")
    db = app.state_db
    rid = await _insert_id(
        db,
        "INSERT INTO services (name, url, owner_id, created) VALUES (?, ?, ?, ?)",
        body.name,
        body.url,
        claims["sub"],
        time.time(),
    )
    for fn in (status_rollup, service_list):
        fn.cache_clear()
    return {"id": int(rid)}, 201


@app.get("/api/incidents")
async def api_incidents(req):
    db = app.state_db
    rows = await db.fetch_all(
        "SELECT id, service_id, title, status, opened FROM incidents ORDER BY id DESC LIMIT 100"
    )
    return {"incidents": [dict(r) for r in rows]}


@app.post("/api/incidents")
async def api_incident_create(req, body: IncidentIn, claims=Depends(require_roles("responder", "admin"))):
    db = app.state_db
    if body.service_id:
        svc = await db.fetch_one("SELECT id FROM services WHERE id = ?", body.service_id)
        if not svc:
            abort(404, "No such service")
    else:
        first = await db.fetch_one("SELECT id FROM services ORDER BY id LIMIT 1")
        body.service_id = first["id"] if first else 0
    iid, _ = await _open_incident(db, body.service_id, body.title, claims["sub"])
    return {"id": iid}, 201


ingest_auth = APIKeyAuth(lookup=_key_lookup)


@app.post("/api/ingest")
async def api_ingest(req, bg: BackgroundTasks, info=Depends(ingest_auth)):
    try:
        payload = await req.json()
    except Exception:
        abort(400, "invalid JSON")
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or len(results) > 500:
        abort(400, "body must be {results: [...]} (max 500)")
    db = app.state_db
    accepted, opened = 0, 0
    for item in results:
        try:
            sid = int(item.get("service_id", 0))
            ok = bool(item.get("ok", False))
            ms = max(0, int(item.get("ms", 0)))
            code = int(item.get("code", 0))
        except (ValueError, TypeError, AttributeError):
            continue
        if not await db.fetch_one("SELECT id FROM services WHERE id = ?", sid):
            continue
        created = await _record_result(db, sid, ok, ms, code, opened_by=f"ingest:{info.get('key_name', '?')}")
        accepted += 1
        if created:
            opened += 1
    bg.add(_ingest_logged, accepted)
    return {"accepted": accepted, "incidents_triggered": opened}, 201


async def _ingest_logged(n: int):
    db = getattr(app, "state_db", None)
    if db is None:
        return
    await db.execute(
        "INSERT INTO notifications (kind, ref, created) VALUES (?, ?, ?)", "ingest", n, time.time()
    )


# ---- live feed ----


@app.websocket("/ws/feed")
async def ws_feed(ws):
    await ws.accept()
    await feed.join(ws)
    try:
        while True:
            await ws.receive_text()  # heartbeats ignored; incidents arrive via broadcast
    except WebSocketDisconnect:
        pass
    finally:
        feed.leave(ws)
