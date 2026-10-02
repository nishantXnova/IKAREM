"""FORGE — a workshop OS for small teams, running on stock IKAREM.

Projects + kanban board + team chat (websockets) + notes wiki + expense
ledger + habit tracker + file shelf + JSON API + CSV exports + durable
queue tasks + scheduled digest. One stdlib-only framework, one SQLite file.

Run:  uvicorn forge.app:app   (or: ikarem run forge.app:app)
Test: python -m pytest forge/tests -q
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

from ikarem import (
    BackgroundTasks,
    BadRequest,
    Blueprint,
    ConcurrencyLimitMiddleware,
    CORSMiddleware,
    CSRFMiddleware,
    Depends,
    Field,
    IdempotencyMiddleware,
    Ikarem,
    MemoryCache,
    MethodView,
    NotFound,
    QueuePlugin,
    RateLimitMiddleware,
    RequestIDMiddleware,
    Schema,
    SecurityHeadersMiddleware,
    SessionMiddleware,
    TimeoutMiddleware,
    Unauthorized,
    check_password,
    csrf_token,
    flash,
    get_flashed_messages,
    hash_password,
    negotiate,
    task,
    verify_token,
)
from ikarem.db import DatabasePlugin
from ikarem.http import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse, UploadFile
from ikarem.static import FileResponse
from ikarem.websocket import Room, WebSocket, WebSocketDisconnect

BASE = Path(__file__).parent
UPLOADS = BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)
MAILBOX = UPLOADS / "mailbox"
MAILBOX.mkdir(exist_ok=True)

CACHE = MemoryCache()
ROOMS: dict[str, Room] = {}

EXPENSE_CATS = ["Materials", "Tools", "Travel", "Food", "Rent", "Fees", "Sales", "Other"]
CAT_DOT = {
    "Materials": "#BC3F0C",
    "Tools": "#31556E",
    "Travel": "#4A6B1F",
    "Food": "#9A6A00",
    "Rent": "#6B3FA0",
    "Fees": "#6F6353",
    "Sales": "#1C7A3D",
    "Other": "#8A8171",
}
BOARD_STATES = ("todo", "doing", "done")
PRI_LABEL = {1: "Low", 2: "Normal", 3: "High"}


# ---------------------------------------------------------------- schemas


class Signup(Schema):
    email: str = Field(..., email=True, max_length=254)
    password: str = Field(..., min_length=8, max_length=128)
    name: str = Field("", max_length=60)


class ProjectIn(Schema):
    name: str = Field(..., min_length=2, max_length=80)
    brief: str = Field("", max_length=500)
    color: str = Field("ember", pattern=r"^(ember|moss|slate|amber|plum)$")


class TaskIn(Schema):
    title: str = Field(..., min_length=2, max_length=160)
    detail: str = Field("", max_length=1000)
    priority: int = Field(2, ge=1, le=3)
    due: str = Field("", pattern=r"^(\d{4}-\d{2}-\d{2})?$")


class NoteIn(Schema):
    title: str = Field(..., min_length=2, max_length=160)
    body: str = Field(..., min_length=1, max_length=20000)


class ExpenseIn(Schema):
    label: str = Field(..., min_length=2, max_length=160)
    amount: float = Field(..., gt=0, le=10_000_000)
    kind: str = Field("expense", pattern=r"^(income|expense)$")
    category: str = Field("Other", min_length=1, max_length=40)
    day: str = Field("", pattern=r"^(\d{4}-\d{2}-\d{2})?$")


class HabitIn(Schema):
    name: str = Field(..., min_length=2, max_length=80)
    target: int = Field(5, ge=1, le=7)


class CommentIn(Schema):
    body: str = Field(..., min_length=1, max_length=2000)


# ---------------------------------------------------------------- durable work


@task("forge-welcome")
async def forge_welcome(payload: dict) -> None:
    """Durable welcome note: survives restarts via the queue table.

    Drain with: ikarem worker forge.app:app
    """
    to = re.sub(r"[^A-Za-z0-9@._-]", "_", str(payload.get("to", "user")))[:80]
    (MAILBOX / f"{to}.txt").write_text(
        f"FORGE shop note — {datetime.now().isoformat(timespec='seconds')}\n"
        f"Welcome, {payload.get('to', 'teammate')}. "
        "Your bench is ready: pick a project, move one card to doing.\n",
        encoding="utf-8",
    )


def create_app(db_url: str = "sqlite:///forge.db", demo: bool = True) -> Ikarem:
    app = Ikarem(
        session_secret="forge-shop-secret-change-in-prod",
        auth_secret="forge-jwt-secret-change-in-prod",
        db_url=db_url,
        demo="true" if demo else "false",
        version="2.0.0",
    )
    app.use(RequestIDMiddleware())
    app.use(SecurityHeadersMiddleware())
    app.use(CORSMiddleware())
    app.use(SessionMiddleware())
    app.use(CSRFMiddleware(exempt_paths=["/api/*"]))
    app.use(RateLimitMiddleware(per_minute=300))
    app.use(TimeoutMiddleware(20))
    app.use(ConcurrencyLimitMiddleware(200))
    app.use(IdempotencyMiddleware())
    # NISH mode: every JSON response also answers NISH when the client asks
    # (?format=nish or Accept mentioning nish), with content-hash ETags so
    # repolls 304. The Viewer extension paints the NISH shape. One-way switch.
    app.nish_mode()
    app.register(DatabasePlugin(db_url))
    try:
        app.register(QueuePlugin())
    except Exception:
        pass
    app.mount_static("/static", str(BASE / "static"))

    api = Blueprint("api", url_prefix="/api")
    _register_api(api)
    app.register_blueprint(api)
    _register_routes(app)
    _register_ws(app)

    @app.on_startup
    async def _init() -> None:
        await init_db(app)

    @app.every(600)
    async def _digest() -> None:
        try:
            db = app.state_db
            row = await db.fetch_one("SELECT COUNT(*) AS n FROM tasks WHERE state != 'done'")
            await db.execute(
                "INSERT INTO activity (user_id, kind, text, created) VALUES (?, ?, ?, ?)",
                "system",
                "digest",
                f"Shop digest: {row['n']} open cards on the floor.",
                _now(),
            )
        except Exception:
            pass

    return app


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------- db


async def init_db(app: Ikarem) -> None:
    db = app.state_db
    await db.execute(
        "CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE,"
        " pw TEXT, name TEXT, role TEXT, created TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " owner_id TEXT, name TEXT, brief TEXT, status TEXT, color TEXT, created TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " project_id INTEGER, title TEXT, detail TEXT, state TEXT, priority INTEGER,"
        " assignee TEXT, due TEXT, created TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " project_id INTEGER, author_id TEXT, title TEXT, body TEXT, updated TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS expenses (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " project_id INTEGER, user_id TEXT, day TEXT, label TEXT, cents INTEGER,"
        " kind TEXT, category TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS habits (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id TEXT, name TEXT, target INTEGER, created TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS habit_checks (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " habit_id INTEGER, day TEXT, UNIQUE(habit_id, day))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " project_id INTEGER, user_id TEXT, name TEXT, body TEXT, created TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " project_id INTEGER, user_id TEXT, name TEXT, body TEXT, created TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " project_id INTEGER, user_id TEXT, name TEXT, stored TEXT, size INTEGER, created TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id TEXT, kind TEXT, text TEXT, created TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id TEXT, name TEXT, prefix TEXT, phash TEXT, created TEXT)"
    )
    demo_on = str(app.config.get("demo", "true")).lower() in ("1", "true", "yes", "on")
    if demo_on and await db.fetch_one("SELECT id FROM users LIMIT 1") is None:
        await _seed_demo(db)


async def _seed_demo(db) -> None:
    import random

    rng = random.Random(42)
    uid = "demo-user"
    await db.execute(
        "INSERT INTO users (id, email, pw, name, role, created) VALUES (?, ?, ?, ?, ?, ?)",
        uid,
        "demo@forge.local",
        hash_password("forge1234"),
        "Shopkeeper",
        "admin",
        _now(),
    )
    specs = [
        ("Market stall build", "Timber counter, awning and till for the Saturday market.", "ember"),
        ("Catalogue No. 4", "Autumn print run: 24 pages, two inks, stitched spine.", "slate"),
        ("Delivery bike", "Rebuild the cargo bike: brakes, box, livery.", "moss"),
    ]
    pids = []
    for name, brief, color in specs:
        pid = await db.execute(
            "INSERT INTO projects (owner_id, name, brief, status, color, created) VALUES (?, ?, ?, ?, ?, ?)",
            uid,
            name,
            brief,
            "open",
            color,
            _now(),
        )
        pids.append(int(pid) if isinstance(pid, int) else _last_id(db, pid))
    cards = [
        ("Cut counter top", "Oak offcut, 180x60, oil finish.", "doing", 3),
        ("Sew awning", "Canvas stripe, brass eyelets.", "todo", 2),
        ("Wire the till", "Second-hand drawer + reader.", "todo", 2),
        ("Price cards", "Letterpress, 40 pieces.", "done", 1),
        ("Lay out pages", "Grid first, ornaments last.", "doing", 3),
        ("Proof read", "Two passes, red pen.", "todo", 2),
        ("Order paper", "300gsm recycled, 200 sheets.", "done", 2),
        ("Strip brakes", "New pads + cables.", "todo", 3),
        ("Build cargo box", "Ply, sealed against rain.", "doing", 2),
        ("Paint livery", "Cream on green, hand letter.", "todo", 1),
    ]
    for i, (title, detail, state, pri) in enumerate(cards):
        pid = pids[i % len(pids)]
        due = (date.today() + timedelta(days=rng.randint(-4, 12))).isoformat()
        await db.execute(
            "INSERT INTO tasks (project_id, title, detail, state, priority, assignee, due, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            pid,
            title,
            detail,
            state if rng.random() > 0.15 else "done",
            pri,
            "Shopkeeper",
            due,
            _now(),
        )
    notes = [
        (pids[0], "Stall dimensions", "Pitch is 3m x 3m. Counter along the back, till right."),
        (pids[1], "Ink order", "Burnt orange + slate. Check stock before Friday."),
        (None, "Shop rules", "Clean the bench. Label every offcut. Tea rota is sacred."),
    ]
    for pid, title, body in notes:
        await db.execute(
            "INSERT INTO notes (project_id, author_id, title, body, updated) VALUES (?, ?, ?, ?, ?)",
            pid,
            uid,
            title,
            body,
            _now(),
        )
    cats = ["Materials", "Tools", "Travel", "Food", "Rent", "Fees", "Sales"]
    for _ in range(34):
        kind = "income" if rng.random() < 0.22 else "expense"
        cat = "Sales" if kind == "income" else cats[rng.randrange(6)]
        day = (date.today() - timedelta(days=rng.randint(0, 80))).isoformat()
        await db.execute(
            "INSERT INTO expenses (project_id, user_id, day, label, cents, kind, category)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            rng.choice(pids),
            uid,
            day,
            f"{cat} — job {rng.randint(100, 999)}",
            rng.randint(400, 60000),
            kind,
            cat,
        )
    for hname, target in [
        ("Sweep the shop", 6),
        ("Sharpen one tool", 3),
        ("Ledger entry", 7),
        ("Walk the dog", 5),
    ]:
        hid = await db.execute(
            "INSERT INTO habits (user_id, name, target, created) VALUES (?, ?, ?, ?)",
            uid,
            hname,
            target,
            _now(),
        )
        hid = int(hid) if isinstance(hid, int) else _last_id(db, hid)
        for d in range(14):
            if rng.random() < 0.62:
                day = (date.today() - timedelta(days=d)).isoformat()
                try:
                    await db.execute("INSERT INTO habit_checks (habit_id, day) VALUES (?, ?)", hid, day)
                except Exception:
                    pass
    for pid in pids:
        await db.execute(
            "INSERT INTO comments (project_id, user_id, name, body, created) VALUES (?, ?, ?, ?, ?)",
            pid,
            uid,
            "Shopkeeper",
            "Kicked off. First card is on the bench.",
            _now(),
        )
    await db.execute(
        "INSERT INTO activity (user_id, kind, text, created) VALUES (?, ?, ?, ?)",
        uid,
        "seed",
        "Shop opened with three jobs on the floor.",
        _now(),
    )


def _last_id(db, ret) -> int:
    try:
        return int(ret)
    except Exception:
        return 0


async def log(db, uid: str, kind: str, text: str) -> None:
    try:
        await db.execute(
            "INSERT INTO activity (user_id, kind, text, created) VALUES (?, ?, ?, ?)",
            uid,
            kind,
            text[:300],
            _now(),
        )
    except Exception:
        pass


# ---------------------------------------------------------------- auth


class LoginRequired(Unauthorized):
    """Anonymous visitor: browsers go to /login, APIs get JSON 401."""


def wants_html(req) -> bool:
    return "text/html" in req.headers.get("accept", "")


async def current_user(req):
    uid = req.session.get("uid")
    if uid:
        return uid
    auth = req.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        try:
            claims = verify_token(auth[7:], req.app.config.get("auth_secret", "change-me"))
            return claims["sub"]
        except Exception:
            pass
    key = req.headers.get("x-api-key", "")
    if key and len(key) > 12:
        prefix = key[:10]
        db = req.app.state_db
        rows = await db.fetch_all("SELECT user_id, phash FROM api_keys WHERE prefix = ?", prefix)
        digest = hashlib.sha256(key.encode()).hexdigest()
        for r in rows:
            if secrets.compare_digest(r["phash"], digest):
                return r["user_id"]
    raise LoginRequired("login required — sign in at /login or pass a Bearer/API token")


async def user_row(req, uid: str) -> dict:
    row = await req.app.state_db.fetch_one("SELECT * FROM users WHERE id = ?", uid)
    if not row:
        raise LoginRequired("account gone — please log in again")
    return row


# ---------------------------------------------------------------- html helpers


def esc(s: object) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return f"{sign}${c // 100:,}.{c % 100:02d}"


NAV = [
    ("dash", "/", "Dashboard"),
    ("projects", "/projects", "Projects"),
    ("tasks", "/tasks", "Tasks"),
    ("notes", "/notes", "Notebook"),
    ("expenses", "/expenses", "Ledger"),
    ("habits", "/habits", "Habits"),
    ("team", "/team", "Crew"),
    ("settings", "/settings", "Settings"),
    ("explorer", "/explorer", "Explorer"),
]


def layout(title: str, body: str, active: str = "", req=None, email: str | None = None) -> HTMLResponse:
    nav = "".join(
        f'<a href="{href}" class="{"on" if active == key else ""}">{label}</a>' for key, href, label in NAV
    )
    user = ""
    csrf = csrf_token(req) if req is not None else ""
    if email:
        user = (
            f'<span class="who">{esc(email)}</span>'
            f'<form method="post" action="/logout" class="inline">'
            f'<input type="hidden" name="_csrf_token" value="{csrf}">'
            f'<button class="btn small ghost">Log out</button></form>'
        )
    else:
        user = '<a class="btn small" href="/login">Log in</a>'
    flashes = ""
    if req is not None:
        msgs = get_flashed_messages(req)
        if msgs:
            flashes = (
                '<div class="flashes">' + "".join(f'<p class="flash">{esc(m)}</p>' for m in msgs) + "</div>"
            )
    search = (
        '<form method="get" action="/search" class="search">'
        '<input name="q" placeholder="Search jobs, cards, notes…" aria-label="Search">'
        '<button class="btn small ghost" aria-label="Search">→</button></form>'
        if email
        else ""
    )
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · FORGE</title>
<link rel="stylesheet" href="/static/style.css">
<link rel="icon" href="/static/favicon.svg"></head>
<body><div class="app"><aside><div class="brand"><span class="mark">◆</span> FORGE</div>
<p class="tag">Workshop OS · Heat 042</p><nav>{nav}</nav>
<div class="side-foot"><a href="/docs">API docs</a><a href="/openapi.json">OpenAPI</a>
<a href="/healthz">Health</a>{user}</div></aside>
<main><div class="topbar"><h1>{esc(title)}</h1>{search}</div>{flashes}{body}</main></div>
<script src="/static/app.js" defer></script></body></html>"""
    )


def auth_page(kind: str, req, err: str = "", email: str = "") -> HTMLResponse:
    is_login = kind == "login"
    title = "Back to the bench" if is_login else "Take a bench"
    swap = (
        'New to the shop? <a href="/register">Register</a> · '
        '<span class="muted">demo: demo@forge.local / forge1234</span>'
        if is_login
        else 'Have a bench? <a href="/login">Log in</a>'
    )
    errh = f'<p class="err">{esc(err)}</p>' if err else ""
    name_field = (
        "" if is_login else '<label>Handle<input name="name" maxlength="60" placeholder="e.g. Ada"></label>'
    )
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{"Log in" if is_login else "Register"} · FORGE</title>
<link rel="stylesheet" href="/static/style.css"></head><body><div class="gate">
<div class="gate-card"><p class="kicker">FORGE · Workshop OS</p><h1>{title}</h1>{errh}
<form method="post" action="{"/login" if is_login else "/register"}" class="form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
{name_field}
<label>Email<input name="email" type="email" value="{esc(email)}" required></label>
<label>Password <small>{"welcome back" if is_login else "8+ characters"}</small>
<input name="password" type="password" required></label>
<button>{"Log in" if is_login else "Claim a bench"}</button></form>
<p class="muted">{swap}</p></div></div></body></html>"""
    )


def bars_svg(days: list[dict]) -> str:
    mx = max([d["n"] for d in days] + [1])
    out = ['<svg viewBox="0 0 420 150" class="chart" role="img" aria-label="Cards finished per day">']
    for i, d in enumerate(days):
        h = round(d["n"] / mx * 100) if mx else 0
        x = 8 + i * 29
        cls = "today" if d["day"] == _today() else ""
        out.append(
            f'<rect x="{x}" y="{125 - h}" width="20" height="{max(h, 3)}" rx="2" class="{cls}">'
            f"<title>{d['day']}: {d['n']} done</title></rect>"
        )
        if i % 2 == 0:
            out.append(f'<text x="{x + 10}" y="140" class="lbl">{d["day"][5:]}</text>')
    return "".join(out) + "</svg>"


def spend_rows(cats: list[dict], total: int) -> str:
    if not total:
        return '<p class="muted">No spend recorded yet.</p>'
    rows = []
    for c in cats:
        pct = (c["total"] / total * 100) if total else 0
        dot = CAT_DOT.get(c["category"], "#8A8171")
        rows.append(
            f'<li><i style="background:{dot}"></i><span>{esc(c["category"])}</span>'
            f'<div class="meter"><b style="width:{pct:.0f}%;background:{dot}"></b></div>'
            f"<b>{money(c['total'])}</b><em>{pct:.0f}%</em></li>"
        )
    return f'<ul class="meters">{"".join(rows)}</ul>'


# ---------------------------------------------------------------- routes


def _register_routes(app: Ikarem) -> None:
    @app.exception_handler(LoginRequired)
    async def _login_required(req, exc):
        if wants_html(req):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": "login required"}, status_code=401)

    # ---- auth ----

    @app.get("/register")
    async def register_page(req):
        return auth_page("register", req)

    @app.post("/register")
    async def register(req, bg: BackgroundTasks):
        is_json = "application/json" in req.headers.get("content-type", "")
        if is_json:
            try:
                data = Signup.validate(await req.json())
            except Exception as e:
                raise BadRequest(f"validation failed: {e}")
        else:
            form = await req.form()
            try:
                data = Signup.validate(
                    {
                        "email": form.get("email", ""),
                        "password": form.get("password", ""),
                        "name": form.get("name", ""),
                    }
                )
            except Exception as e:
                return auth_page("register", req, f"Fix this: {e}", form.get("email", ""))
        uid = secrets.token_hex(8)
        try:
            await req.app.state_db.execute(
                "INSERT INTO users (id, email, pw, name, role, created) VALUES (?, ?, ?, ?, ?, ?)",
                uid,
                data.email,
                hash_password(data.password),
                data.name or data.email.split("@")[0],
                "member",
                _now(),
            )
        except Exception:
            existing = await req.app.state_db.fetch_one("SELECT id FROM users WHERE email = ?", data.email)
            if existing is None:
                raise
            if is_json:
                raise BadRequest("email already registered — try /login instead")
            return auth_page("register", req, "That email has a bench already.", data.email)
        req.session["uid"] = uid
        bg.add(print, f"forge: welcome {data.email}")
        try:
            if hasattr(req.app, "state_queue"):
                await req.app.state_queue.enqueue("forge-welcome", {"to": data.email})
        except Exception:
            pass
        flash(req, f"Bench claimed — welcome, {data.email}.")
        if "text/html" in req.headers.get("accept", ""):
            return RedirectResponse("/", status_code=303)
        return {"uid": uid, "email": data.email}, 201

    @app.get("/login")
    async def login_page(req):
        return auth_page("login", req)

    @app.post("/login")
    async def login(req):
        is_json = "application/json" in req.headers.get("content-type", "")
        if is_json:
            body = await req.json()
            email, pw = body.get("email", ""), body.get("password", "")
        else:
            form = await req.form()
            email, pw = form.get("email", ""), form.get("password", "")
        row = await req.app.state_db.fetch_one("SELECT * FROM users WHERE email = ?", email)
        if not row or not check_password(pw, row["pw"]):
            if is_json:
                raise Unauthorized("bad credentials — check email/password")
            return auth_page("login", req, "Wrong email or password.", email)
        req.session["uid"] = row["id"]
        flash(req, f"Back to the bench, {row['name']}.")
        if "text/html" in req.headers.get("accept", ""):
            return RedirectResponse("/", status_code=303)
        return {"uid": row["id"], "email": row["email"]}

    @app.post("/logout")
    async def logout(req):
        req.session.clear()
        flash(req, "Logged out. Bench swept.")
        ctype = req.headers.get("content-type", "")
        if "text/html" in req.headers.get("accept", "") or "multipart" in ctype:
            return RedirectResponse("/login", status_code=303)
        return {"ok": True}

    @app.get("/api/csrf")
    async def api_csrf(req):
        return {"csrf": csrf_token(req)}

    # ---- dashboard ----

    @app.get("/")
    async def dashboard(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        db = req.app.state_db
        cache_key = f"dash:{uid}"
        cached = await CACHE.get(cache_key)
        if not isinstance(cached, dict):
            open_n = (await db.fetch_one("SELECT COUNT(*) AS n FROM tasks WHERE state != 'done'"))["n"]
            done_wk = (
                await db.fetch_one(
                    "SELECT COUNT(*) AS n FROM tasks WHERE state = 'done' AND created >= ?",
                    (date.today() - timedelta(days=7)).isoformat(),
                )
            )["n"]
            month = _today()[:7]
            spend = await db.fetch_one(
                "SELECT COALESCE(SUM(CASE WHEN kind='expense' THEN cents END),0) AS e,"
                " COALESCE(SUM(CASE WHEN kind='income' THEN cents END),0) AS i"
                " FROM expenses WHERE substr(day,1,7) = ?",
                month,
            )
            bycat = await db.fetch_all(
                "SELECT category, SUM(cents) AS total FROM expenses"
                " WHERE kind='expense' AND substr(day,1,7) = ? GROUP BY category ORDER BY total DESC",
                month,
            )
            days = []
            for d in range(13, -1, -1):
                day = (date.today() - timedelta(days=d)).isoformat()
                n = (await db.fetch_one("SELECT COUNT(*) AS n FROM habit_checks WHERE day = ?", day))["n"]
                days.append({"day": day, "n": n})
            projects = await db.fetch_all(
                "SELECT p.*, (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id"
                " AND t.state != 'done') AS open_n FROM projects p ORDER BY p.id DESC LIMIT 6"
            )
            cached = {
                "open": open_n,
                "done_wk": done_wk,
                "spent": spend["e"],
                "earned": spend["i"],
                "bycat": [dict(c) for c in bycat],
                "days": days,
                "projects": [dict(p) for p in projects],
            }
            await CACHE.set(cache_key, cached, ttl=30)
        activity = await db.fetch_all("SELECT * FROM activity ORDER BY id DESC LIMIT 8")
        habits = await db.fetch_all("SELECT * FROM habits WHERE user_id = ? ORDER BY id", uid)
        checks = await db.fetch_all("SELECT habit_id FROM habit_checks WHERE day = ?", _today())
        done_today = {c["habit_id"] for c in checks}
        habit_line = (
            "".join(
                f'<span class="tick {"on" if h["id"] in done_today else ""}">'
                f"{'●' if h['id'] in done_today else '○'} {esc(h['name'])}</span>"
                for h in habits
            )
            or '<span class="muted">No habits yet — <a href="/habits">set one</a>.</span>'
        )
        proj_cards = (
            "".join(
                f'<a class="job dot-{esc(p["color"])}" href="/projects/{p["id"]}">'
                f"<b>{esc(p['name'])}</b><span>{p['open_n']} open cards</span></a>"
                for p in cached["projects"]
            )
            or '<p class="muted">No jobs yet. <a href="/projects/new">Start one</a>.</p>'
        )
        feed = (
            "".join(
                f'<li><span class="muted">{esc(a["created"])}</span> {esc(a["text"])}</li>' for a in activity
            )
            or '<li class="muted">Quiet shop. Do something worth logging.</li>'
        )
        body = f"""
<div class="cards">
<div class="card"><span>Open cards</span><b>{cached["open"]}</b><em>across all jobs</em></div>
<div class="card"><span>Done · 7 days</span><b>{cached["done_wk"]}</b><em>off the bench</em></div>
<div class="card"><span>Spent · {_today()[:7]}</span><b class="neg">{money(cached["spent"])}</b>
<em>earned {money(cached["earned"])}</em></div>
</div>
<div class="grid">
<div class="panel"><h2>Shop rhythm · habit checks, 14 days</h2>{bars_svg(cached["days"])}
<div class="ticks">{habit_line}</div></div>
<div class="panel"><h2>Spend by shelf · {_today()[:7]}</h2>{spend_rows(cached["bycat"], cached["spent"])}</div>
</div>
<div class="grid">
<div class="panel"><h2>Jobs on the floor</h2><div class="jobs">{proj_cards}</div></div>
<div class="panel"><h2>Shop log</h2><ul class="feed">{feed}</ul></div>
</div>"""
        return layout("Dashboard", body, "dash", req=req, email=me["email"])

    # ---- projects ----

    @app.get("/projects")
    async def projects_page(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        rows = await req.app.state_db.fetch_all(
            "SELECT p.*, (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.state != 'done') AS open_n,"
            " (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.state = 'done') AS done_n"
            " FROM projects p ORDER BY p.id DESC"
        )
        cards = (
            "".join(
                f'<a class="job big dot-{esc(p["color"])}" href="/projects/{p["id"]}">'
                f"<b>{esc(p['name'])}</b><span>{esc(p['brief'] or '—')}</span>"
                f"<em>{p['open_n']} open · {p['done_n']} done · {esc(p['status'])}</em></a>"
                for p in rows
            )
            or '<p class="muted">Empty floor.</p>'
        )
        body = f'<p><a class="btn" href="/projects/new">+ Start a job</a></p><div class="jobs">{cards}</div>'
        return layout("Projects", body, "projects", req=req, email=me["email"])

    @app.get("/projects/new")
    async def project_new(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        opts = "".join(
            f'<option value="{c}">{c.title()}</option>' for c in ("ember", "moss", "slate", "amber", "plum")
        )
        body = f"""<div class="panel"><form method="post" action="/projects" class="form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<label>Job name<input name="name" required maxlength="80" placeholder="e.g. Market stall build"></label>
<label>Brief<textarea name="brief" rows="3" maxlength="500" placeholder="What does done look like?"></textarea></label>
<label>Tag colour<select name="color">{opts}</select></label>
<button>Put it on the floor</button></form></div>"""
        return layout("New job", body, "projects", req=req, email=me["email"])

    @app.post("/projects")
    async def project_create(req, uid=Depends(current_user)):
        form = await req.form()
        try:
            data = ProjectIn.validate(
                {
                    "name": form.get("name", ""),
                    "brief": form.get("brief", ""),
                    "color": form.get("color", "ember"),
                }
            )
        except Exception as e:
            raise BadRequest(f"fix the form and retry: {e}")
        pid = await req.app.state_db.execute(
            "INSERT INTO projects (owner_id, name, brief, status, color, created) VALUES (?, ?, ?, ?, ?, ?)",
            uid,
            data.name,
            data.brief,
            "open",
            data.color,
            _now(),
        )
        await CACHE.delete(f"dash:{uid}")
        await log(req.app.state_db, uid, "project", f"Started job “{data.name}”.")
        flash(req, f"Job “{data.name}” is on the floor.")
        return RedirectResponse(f"/projects/{pid}", status_code=303)

    async def _project_or_404(db, pid: int) -> dict:
        row = await db.fetch_one("SELECT * FROM projects WHERE id = ?", pid)
        if not row:
            raise NotFound(f"no job #{pid} — check /projects for the list")
        return row

    @app.get("/projects/{pid:int}")
    async def project_detail(req, pid: int, uid=Depends(current_user)):
        me = await user_row(req, uid)
        db = req.app.state_db
        p = await _project_or_404(db, pid)
        tasks = await db.fetch_all(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY"
            " CASE state WHEN 'doing' THEN 0 WHEN 'todo' THEN 1 ELSE 2 END, priority DESC, id",
            pid,
        )
        notes = await db.fetch_all("SELECT * FROM notes WHERE project_id = ? ORDER BY id DESC LIMIT 5", pid)
        comments = await db.fetch_all(
            "SELECT * FROM comments WHERE project_id = ? ORDER BY id DESC LIMIT 10", pid
        )
        files = await db.fetch_all("SELECT * FROM files WHERE project_id = ? ORDER BY id DESC", pid)
        counts = {s: 0 for s in BOARD_STATES}
        for t in tasks:
            counts[t["state"]] = counts.get(t["state"], 0) + 1
        task_list = (
            "".join(
                f'<li class="st-{t["state"]}"><b>[{esc(PRI_LABEL.get(t["priority"], "?"))}]</b> '
                f"{esc(t['title'])} <span class='muted'>{esc(t['state'])}"
                f"{' · due ' + esc(t['due']) if t['due'] else ''}</span></li>"
                for t in tasks[:10]
            )
            or '<li class="muted">No cards yet.</li>'
        )
        note_list = (
            "".join(f'<li><a href="/notes/{n["id"]}">{esc(n["title"])}</a></li>' for n in notes)
            or '<li class="muted">No notes pinned.</li>'
        )
        talk = (
            "".join(
                f'<li><b>{esc(c["name"])}</b> <span class="muted">{esc(c["created"])}</span><br>{esc(c["body"])}</li>'
                for c in comments
            )
            or '<li class="muted">No remarks yet.</li>'
        )
        file_list = (
            "".join(
                f'<li><a href="/files/{f["id"]}">{esc(f["name"])}</a>'
                f' <span class="muted">{f["size"]} bytes</span></li>'
                for f in files
            )
            or '<li class="muted">Shelf is empty.</li>'
        )
        body = f"""
<p class="kicker dot-{esc(p["color"])}">JOB #{p["id"]} · {esc(p["status"]).upper()}</p>
<p class="lede">{esc(p["brief"] or "No brief written.")}</p>
<p class="rowbtns"><a class="btn" href="/projects/{pid}/board">Open board</a>
<a class="btn ghost" href="/projects/{pid}/chat">Team chat</a>
<a class="btn ghost" href="/projects/{pid}/files">File shelf</a></p>
<div class="grid3">
<div class="panel"><h2>Cards · {counts["todo"]} to do / {counts["doing"]} doing / {counts["done"]} done</h2>
<ul class="plain">{task_list}</ul></div>
<div class="panel"><h2>Pinned notes</h2><ul class="plain">{note_list}</ul>
<h2 style="margin-top:16px">On the shelf</h2><ul class="plain">{file_list}</ul></div>
<div class="panel"><h2>Remarks</h2><ul class="plain">{talk}</ul>
<form method="post" action="/projects/{pid}/comments" class="form" style="margin-top:10px">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<label>Say it<input name="body" required maxlength="2000" placeholder="Note for the crew…"></label>
<button class="small">Pin remark</button></form></div>
</div>"""
        return layout(p["name"], body, "projects", req=req, email=me["email"])

    @app.post("/projects/{pid:int}/comments")
    async def project_comment(req, pid: int, uid=Depends(current_user)):
        me = await user_row(req, uid)
        await _project_or_404(req.app.state_db, pid)
        form = await req.form()
        try:
            data = CommentIn.validate({"body": form.get("body", "")})
        except Exception as e:
            raise BadRequest(f"fix the remark and retry: {e}")
        await req.app.state_db.execute(
            "INSERT INTO comments (project_id, user_id, name, body, created) VALUES (?, ?, ?, ?, ?)",
            pid,
            uid,
            me["name"],
            data.body,
            _now(),
        )
        return RedirectResponse(f"/projects/{pid}", status_code=303)

    @app.post("/projects/{pid:int}/status")
    async def project_status(req, pid: int, uid=Depends(current_user)):
        await _project_or_404(req.app.state_db, pid)
        form = await req.form()
        status = form.get("status", "open")
        if status not in ("open", "shipped", "paused"):
            raise BadRequest("status must be open, shipped or paused")
        await req.app.state_db.execute("UPDATE projects SET status = ? WHERE id = ?", status, pid)
        await CACHE.delete(f"dash:{uid}")
        return RedirectResponse(f"/projects/{pid}", status_code=303)

    @app.post("/projects/{pid:int}/delete")
    async def project_delete(req, pid: int, uid=Depends(current_user)):
        await _project_or_404(req.app.state_db, pid)
        db = req.app.state_db
        for table in ("tasks", "notes", "comments", "chat", "files", "expenses"):
            try:
                await db.execute(f"UPDATE {table} SET project_id = NULL WHERE project_id = ?", pid)
            except Exception:
                await db.execute(f"DELETE FROM {table} WHERE project_id = ?", pid)
        await db.execute("DELETE FROM projects WHERE id = ?", pid)
        await CACHE.delete(f"dash:{uid}")
        flash(req, f"Job #{pid} struck from the floor.")
        return RedirectResponse("/projects", status_code=303)

    # ---- board + tasks ----

    @app.get("/projects/{pid:int}/board")
    async def board(req, pid: int, uid=Depends(current_user)):
        me = await user_row(req, uid)
        p = await _project_or_404(req.app.state_db, pid)
        tasks = await req.app.state_db.fetch_all(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY priority DESC, id", pid
        )
        cols = []
        for state in BOARD_STATES:
            cards = (
                "".join(
                    f'<div class="tcard p{t["priority"]}"><b>{esc(t["title"])}</b>'
                    f"<span>{esc(t['detail'] or '')}</span>"
                    f"<em>{'★ ' * t['priority']}{esc(t['assignee'] or 'unassigned')}"
                    f"{' · due ' + esc(t['due']) if t['due'] else ''}</em>"
                    f'<div class="tbtns">'
                    + (
                        f'<form method="post" action="/tasks/{t["id"]}/move" class="inline">'
                        f'<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">'
                        f'<input type="hidden" name="to" value="{BOARD_STATES[BOARD_STATES.index(state) - 1]}">'
                        f'<button class="mini" title="Move back">←</button></form>'
                        if state != "todo"
                        else ""
                    )
                    + (
                        f'<form method="post" action="/tasks/{t["id"]}/move" class="inline">'
                        f'<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">'
                        f'<input type="hidden" name="to" value="{BOARD_STATES[BOARD_STATES.index(state) + 1]}">'
                        f'<button class="mini" title="Move on">→</button></form>'
                        if state != "done"
                        else ""
                    )
                    + f'<form method="post" action="/tasks/{t["id"]}/delete" class="inline">'
                    f'<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">'
                    f'<button class="mini danger" title="Strike card">×</button></form>'
                    f"</div></div>"
                    for t in tasks
                    if t["state"] == state
                )
                or '<p class="muted pad">Empty.</p>'
            )
            cols.append(
                f'<div class="col"><h2>{state.upper()} · '
                f"{sum(1 for t in tasks if t['state'] == state)}</h2>{cards}</div>"
            )
        body = f"""
<p><a href="/projects/{pid}">← {esc(p["name"])}</a></p>
<div class="panel"><form method="post" action="/tasks" class="form inline-form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<input type="hidden" name="project_id" value="{pid}">
<input name="title" required maxlength="160" placeholder="New card — e.g. Cut counter top">
<input name="due" type="date">
<select name="priority"><option value="1">Low</option><option value="2" selected>Normal</option>
<option value="3">High</option></select>
<button class="small">Add card</button></form></div>
<div class="board">{"".join(cols)}</div>"""
        return layout(f"Board · {p['name']}", body, "projects", req=req, email=me["email"])

    @app.post("/tasks")
    async def task_create(req, uid=Depends(current_user)):
        form = await req.form()
        try:
            data = TaskIn.validate(
                {
                    "title": form.get("title", ""),
                    "detail": form.get("detail", ""),
                    "priority": form.get("priority", "2"),
                    "due": form.get("due", "") or "",
                }
            )
            pid = int(form.get("project_id", "0") or 0)
        except Exception as e:
            raise BadRequest(f"fix the card and retry: {e}")
        if pid:
            await _project_or_404(req.app.state_db, pid)
        tid = await req.app.state_db.execute(
            "INSERT INTO tasks (project_id, title, detail, state, priority, assignee, due, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            pid or None,
            data.title,
            data.detail,
            "todo",
            int(data.priority),
            "",
            data.due,
            _now(),
        )
        await CACHE.delete(f"dash:{uid}")
        await log(req.app.state_db, uid, "task", f"Card #{tid} cut: “{data.title}”.")
        back = f"/projects/{pid}/board" if pid else "/tasks"
        return RedirectResponse(back, status_code=303)

    @app.post("/tasks/{tid:int}/move")
    async def task_move(req, tid: int, uid=Depends(current_user)):
        row = await req.app.state_db.fetch_one("SELECT * FROM tasks WHERE id = ?", tid)
        if not row:
            raise NotFound(f"no card #{tid}")
        form = await req.form()
        to = form.get("to", "")
        if to not in BOARD_STATES:
            raise BadRequest("destination must be todo, doing or done")
        await req.app.state_db.execute("UPDATE tasks SET state = ? WHERE id = ?", to, tid)
        await CACHE.delete(f"dash:{uid}")
        await log(req.app.state_db, uid, "task", f"Card #{tid} moved to {to}.")
        pid = row["project_id"]
        return RedirectResponse(f"/projects/{pid}/board" if pid else "/tasks", status_code=303)

    @app.post("/tasks/{tid:int}/delete")
    async def task_delete(req, tid: int, uid=Depends(current_user)):
        row = await req.app.state_db.fetch_one("SELECT * FROM tasks WHERE id = ?", tid)
        if not row:
            raise NotFound(f"no card #{tid}")
        await req.app.state_db.execute("DELETE FROM tasks WHERE id = ?", tid)
        await CACHE.delete(f"dash:{uid}")
        pid = row["project_id"]
        return RedirectResponse(f"/projects/{pid}/board" if pid else "/tasks", status_code=303)

    @app.get("/tasks")
    async def tasks_page(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        state = req.query.get("state", "")
        pri = req.query.get("pri", "")
        where, args = ["1 = 1"], []
        if state in BOARD_STATES:
            where.append("t.state = ?")
            args.append(state)
        if pri in ("1", "2", "3"):
            where.append("t.priority = ?")
            args.append(int(pri))
        rows = await req.app.state_db.fetch_all(
            "SELECT t.*, p.name AS pname FROM tasks t LEFT JOIN projects p ON p.id = t.project_id"
            f" WHERE {' AND '.join(where)} ORDER BY t.state != 'done', t.priority DESC, t.id DESC LIMIT 100",
            *args,
        )
        items = (
            "".join(
                f'<tr><td class="d">#{t["id"]}</td><td>{esc(t["title"])}'
                f'<br><span class="muted">{esc(t["pname"] or "loose card")}</span></td>'
                f'<td><span class="pill">{esc(t["state"])}</span></td>'
                f'<td class="r">{"★" * t["priority"]}</td>'
                f'<td class="d">{esc(t["due"] or "—")}</td></tr>'
                for t in rows
            )
            or '<tr><td colspan="5" class="muted">Nothing on the bench.</td></tr>'
        )
        body = f"""
<form method="get" action="/tasks" class="filters">
<select name="state"><option value="">All states</option>
{"".join(f'<option value="{s}"{" selected" if state == s else ""}>{s}</option>' for s in BOARD_STATES)}</select>
<select name="pri"><option value="">Any priority</option>
{"".join(f'<option value="{i}"{" selected" if pri == str(i) else ""}>{PRI_LABEL[i]}</option>' for i in (1, 2, 3))}</select>
<button class="small">Filter</button></form>
<div class="panel"><table>{items}</table></div>"""
        return layout("All cards", body, "tasks", req=req, email=me["email"])

    # ---- files ----

    @app.get("/projects/{pid:int}/files")
    async def files_page(req, pid: int, uid=Depends(current_user)):
        me = await user_row(req, uid)
        p = await _project_or_404(req.app.state_db, pid)
        files = await req.app.state_db.fetch_all(
            "SELECT * FROM files WHERE project_id = ? ORDER BY id DESC", pid
        )
        rows = (
            "".join(
                f'<li><a href="/files/{f["id"]}">{esc(f["name"])}</a>'
                f' <span class="muted">{f["size"]} bytes · {esc(f["created"])}</span></li>'
                for f in files
            )
            or '<li class="muted">Nothing shelved.</li>'
        )
        body = f"""<p><a href="/projects/{pid}">← {esc(p["name"])}</a></p>
<div class="panel"><form method="post" action="/projects/{pid}/files" enctype="multipart/form-data" class="form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<label>Shelve a file <small>up to 8 MB</small><input type="file" name="doc" required></label>
<button class="small">Shelve it</button></form></div>
<div class="panel"><h2>On this shelf</h2><ul class="plain">{rows}</ul></div>"""
        return layout(f"Files · {p['name']}", body, "projects", req=req, email=me["email"])

    @app.post("/projects/{pid:int}/files")
    async def file_upload(req, pid: int, uid=Depends(current_user)):
        await _project_or_404(req.app.state_db, pid)
        form = await req.form(max_file_size=8 * 1024 * 1024)
        f = form.get("doc")
        if not isinstance(f, UploadFile) or not f.filename:
            raise BadRequest("choose a file first, then shelve it")
        data = await f.read()
        if not data:
            raise BadRequest("that file is empty — nothing to shelve")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", f.filename)[-60:] or "file"
        stored = f"{secrets.token_hex(6)}_{safe}"
        (UPLOADS / stored).write_bytes(data)
        await req.app.state_db.execute(
            "INSERT INTO files (project_id, user_id, name, stored, size, created) VALUES (?, ?, ?, ?, ?, ?)",
            pid,
            uid,
            f.filename[:120],
            stored,
            len(data),
            _now(),
        )
        flash(req, f"“{f.filename}” shelved under job #{pid}.")
        return RedirectResponse(f"/projects/{pid}/files", status_code=303)

    @app.get("/files/{fid:int}")
    async def file_get(req, fid: int, uid=Depends(current_user)):
        row = await req.app.state_db.fetch_one("SELECT * FROM files WHERE id = ?", fid)
        if not row:
            raise NotFound(f"no file #{fid} on any shelf")
        path = UPLOADS / row["stored"]
        if not path.is_file():
            raise NotFound("the shelf entry exists but the file is gone — re-upload it")
        return FileResponse(str(path))

    # ---- chat ----

    @app.get("/projects/{pid:int}/chat")
    async def chat_page(req, pid: int, uid=Depends(current_user)):
        me = await user_row(req, uid)
        p = await _project_or_404(req.app.state_db, pid)
        lines = await req.app.state_db.fetch_all(
            "SELECT * FROM chat WHERE project_id = ? ORDER BY id DESC LIMIT 30", pid
        )
        hist = (
            "".join(
                f'<li><b>{esc(c["name"])}</b> <span class="muted">{esc(c["created"])}</span><br>{esc(c["body"])}</li>'
                for c in reversed(list(lines))
            )
            or '<li class="muted">Silent shop. Say the first word.</li>'
        )
        body = f"""<p><a href="/projects/{pid}">← {esc(p["name"])}</a></p>
<div class="panel"><ul class="plain" id="talk">{hist}</ul>
<form class="form inline-form" id="chatform">
<input id="chatbox" maxlength="2000" placeholder="Message the crew…" autocomplete="off">
<button class="small">Send</button></form>
<p class="muted">Live via websocket <code>/ws/hall</code> · history is kept per job.</p></div>
<script>window.FORGE_CHAT = {{project: {pid}, name: {json.dumps(me["name"])}}};</script>"""
        return layout(f"Chat · {p['name']}", body, "projects", req=req, email=me["email"])

    # ---- notes ----

    @app.get("/notes")
    async def notes_page(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        rows = await req.app.state_db.fetch_all(
            "SELECT n.*, p.name AS pname FROM notes n LEFT JOIN projects p ON p.id = n.project_id"
            " ORDER BY n.id DESC"
        )
        items = (
            "".join(
                f'<a class="job" href="/notes/{n["id"]}"><b>{esc(n["title"])}</b>'
                f"<span>{esc((n['body'] or '')[:110])}</span>"
                f"<em>{esc(n['pname'] or 'shop-wide')} · {esc(n['updated'])}</em></a>"
                for n in rows
            )
            or '<p class="muted">Blank pages.</p>'
        )
        body = f'<p><a class="btn" href="/notes/new">+ New note</a></p><div class="jobs">{items}</div>'
        return layout("Notebook", body, "notes", req=req, email=me["email"])

    @app.get("/notes/new")
    async def note_new(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        projects = await req.app.state_db.fetch_all("SELECT id, name FROM projects ORDER BY name")
        opts = '<option value="">Shop-wide</option>' + "".join(
            f'<option value="{p["id"]}">{esc(p["name"])}</option>' for p in projects
        )
        body = f"""<div class="panel"><form method="post" action="/notes" class="form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<label>Title<input name="title" required maxlength="160"></label>
<label>Pinned to<select name="project_id">{opts}</select></label>
<label>Body<textarea name="body" rows="10" required maxlength="20000"></textarea></label>
<button>Pin it up</button></form></div>"""
        return layout("New note", body, "notes", req=req, email=me["email"])

    @app.post("/notes")
    async def note_create(req, uid=Depends(current_user)):
        form = await req.form()
        try:
            data = NoteIn.validate({"title": form.get("title", ""), "body": form.get("body", "")})
            raw_pid = (form.get("project_id", "") or "").strip()
            pid = int(raw_pid) if raw_pid else None
        except Exception as e:
            raise BadRequest(f"fix the note and retry: {e}")
        nid = await req.app.state_db.execute(
            "INSERT INTO notes (project_id, author_id, title, body, updated) VALUES (?, ?, ?, ?, ?)",
            pid,
            uid,
            data.title,
            data.body,
            _now(),
        )
        await log(req.app.state_db, uid, "note", f"Note pinned: “{data.title}”.")
        return RedirectResponse(f"/notes/{nid}", status_code=303)

    @app.get("/notes/{nid:int}")
    async def note_detail(req, nid: int, uid=Depends(current_user)):
        me = await user_row(req, uid)
        row = await req.app.state_db.fetch_one("SELECT * FROM notes WHERE id = ?", nid)
        if not row:
            raise NotFound(f"no note #{nid}")
        paras = "".join(f"<p>{esc(chunk)}</p>" for chunk in row["body"].split("\n\n"))
        body = f"""<p class="muted">Last set {esc(row["updated"])}</p>
<div class="panel prose">{paras}</div>
<div class="panel"><form method="post" action="/notes/{nid}/edit" class="form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<label>Title<input name="title" value="{esc(row["title"])}" required maxlength="160"></label>
<label>Body<textarea name="body" rows="10" required maxlength="20000">{esc(row["body"])}</textarea></label>
<p class="rowbtns"><button class="small">Save</button></p></form>
<form method="post" action="/notes/{nid}/delete" class="inline">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<button class="small danger">Tear out</button></form></div>"""
        return layout(row["title"], body, "notes", req=req, email=me["email"])

    @app.post("/notes/{nid:int}/edit")
    async def note_edit(req, nid: int, uid=Depends(current_user)):
        row = await req.app.state_db.fetch_one("SELECT * FROM notes WHERE id = ?", nid)
        if not row:
            raise NotFound(f"no note #{nid}")
        form = await req.form()
        try:
            data = NoteIn.validate({"title": form.get("title", ""), "body": form.get("body", "")})
        except Exception as e:
            raise BadRequest(f"fix the note and retry: {e}")
        await req.app.state_db.execute(
            "UPDATE notes SET title = ?, body = ?, updated = ? WHERE id = ?",
            data.title,
            data.body,
            _now(),
            nid,
        )
        return RedirectResponse(f"/notes/{nid}", status_code=303)

    @app.post("/notes/{nid:int}/delete")
    async def note_delete(req, nid: int, uid=Depends(current_user)):
        await req.app.state_db.execute("DELETE FROM notes WHERE id = ?", nid)
        return RedirectResponse("/notes", status_code=303)

    # ---- expenses ----

    @app.get("/expenses")
    async def expenses_page(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        db = req.app.state_db
        q = req.query.get("q", "")
        cat = req.query.get("cat", "")
        month = req.query.get("month", "")
        where, args = ["1 = 1"], []
        if q:
            where.append("label LIKE ?")
            args.append(f"%{q}%")
        if cat:
            where.append("category = ?")
            args.append(cat)
        if re.fullmatch(r"\d{4}-\d{2}", month or ""):
            where.append("substr(day, 1, 7) = ?")
            args.append(month)
        w = " AND ".join(where)
        rows = await db.fetch_all(
            f"SELECT * FROM expenses WHERE {w} ORDER BY day DESC, id DESC LIMIT 80", *args
        )
        sums = await db.fetch_one(
            f"SELECT COALESCE(SUM(CASE WHEN kind='income' THEN cents END),0) AS i,"
            f" COALESCE(SUM(CASE WHEN kind='expense' THEN cents END),0) AS e FROM expenses WHERE {w}",
            *args,
        )
        lines = (
            "".join(
                f'<tr><td class="d">{esc(r["day"])}</td><td>{esc(r["label"])}</td>'
                f'<td><span class="pill"><i class="dot" style="background:{CAT_DOT.get(r["category"], "#8A8171")}">'
                f"</i>{esc(r['category'])}</span></td>"
                f'<td class="r {"pos" if r["kind"] == "income" else "neg"}">'
                f"{money(r['cents']) if r['kind'] == 'income' else money(-r['cents'])}</td>"
                f'<td><form method="post" action="/expenses/{r["id"]}/delete" class="inline">'
                f'<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">'
                f'<button class="mini danger">×</button></form></td></tr>'
                for r in rows
            )
            or '<tr><td colspan="5" class="muted">No entries.</td></tr>'
        )
        opts = "".join(
            f'<option value="{c}"{" selected" if cat == c else ""}>{c}</option>' for c in EXPENSE_CATS
        )
        body = f"""
<div class="cards">
<div class="card"><span>Balance · filter</span><b>{money(sums["i"] - sums["e"])}</b></div>
<div class="card"><span>In</span><b class="pos">{money(sums["i"])}</b></div>
<div class="card"><span>Out</span><b class="neg">{money(sums["e"])}</b></div></div>
<div class="panel"><form method="post" action="/expenses" class="form inline-form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<input name="label" required maxlength="160" placeholder="Label — e.g. Oak boards">
<input name="amount" required placeholder="12.50">
<select name="kind"><option value="expense">Out</option><option value="income">In</option></select>
<select name="category">{opts}</select><input name="day" type="date">
<button class="small">Book it</button></form></div>
<form method="get" action="/expenses" class="filters">
<input name="q" placeholder="Search…" value="{esc(q)}">
<select name="cat"><option value="">All shelves</option>{opts}</select>
<input name="month" type="month" value="{esc(month)}"><button class="small">Filter</button>
<a class="btn small ghost" href="/export/expenses.csv">CSV ↓</a></form>
<div class="panel"><table>{lines}</table></div>"""
        return layout("Ledger", body, "expenses", req=req, email=me["email"])

    @app.post("/expenses")
    async def expense_create(req, uid=Depends(current_user)):
        form = await req.form()
        try:
            data = ExpenseIn.validate(
                {
                    "label": form.get("label", ""),
                    "amount": form.get("amount", ""),
                    "kind": form.get("kind", "expense"),
                    "category": form.get("category", "Other"),
                    "day": form.get("day", "") or "",
                }
            )
        except Exception as e:
            raise BadRequest(f"fix the entry and retry: {e}")
        await req.app.state_db.execute(
            "INSERT INTO expenses (project_id, user_id, day, label, cents, kind, category)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            None,
            uid,
            data.day or _today(),
            data.label,
            int(round(float(data.amount) * 100)),
            data.kind,
            data.category,
        )
        await CACHE.delete(f"dash:{uid}")
        flash(req, "Booked.")
        return RedirectResponse("/expenses", status_code=303)

    @app.post("/expenses/{eid:int}/delete")
    async def expense_delete(req, eid: int, uid=Depends(current_user)):
        await req.app.state_db.execute("DELETE FROM expenses WHERE id = ?", eid)
        await CACHE.delete(f"dash:{uid}")
        return RedirectResponse("/expenses", status_code=303)

    @app.get("/export/expenses.csv")
    async def export_expenses(req, uid=Depends(current_user)):
        rows = await req.app.state_db.fetch_all(
            "SELECT day, label, cents, kind, category FROM expenses ORDER BY day, id"
        )

        def gen():
            yield "date,label,cents,kind,category\n"
            for r in rows:
                label = '"' + r["label"].replace('"', '""') + '"'
                yield f"{r['day']},{label},{r['cents']},{r['kind']},{r['category']}\n"

        resp = StreamingResponse(gen(), media_type="text/csv")
        resp.headers["content-disposition"] = "attachment; filename=forge-ledger.csv"
        return resp

    @app.get("/export/tasks.csv")
    async def export_tasks(req, uid=Depends(current_user)):
        rows = await req.app.state_db.fetch_all(
            "SELECT t.id, p.name AS job, t.title, t.state, t.priority, t.due FROM tasks t"
            " LEFT JOIN projects p ON p.id = t.project_id ORDER BY t.id"
        )

        def gen():
            yield "id,job,title,state,priority,due\n"
            for r in rows:
                title = '"' + (r["title"] or "").replace('"', '""') + '"'
                yield f"{r['id']},{r['job'] or ''},{title},{r['state']},{r['priority']},{r['due'] or ''}\n"

        resp = StreamingResponse(gen(), media_type="text/csv")
        resp.headers["content-disposition"] = "attachment; filename=forge-tasks.csv"
        return resp

    # ---- habits ----

    @app.get("/habits")
    async def habits_page(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        habits = await req.app.state_db.fetch_all("SELECT * FROM habits WHERE user_id = ? ORDER BY id", uid)
        start = date.today() - timedelta(days=13)
        all_checks = await req.app.state_db.fetch_all("SELECT habit_id, day FROM habit_checks")
        have = {(c["habit_id"], c["day"]) for c in all_checks}
        blocks = []
        for h in habits:
            cells = []
            streak = 0
            d = date.today()
            while (h["id"], d.isoformat()) in have:
                streak += 1
                d -= timedelta(days=1)
            for i in range(14):
                day = (start + timedelta(days=i)).isoformat()
                on = (h["id"], day) in have
                today_cls = " today" if day == _today() else ""
                cells.append(
                    f'<span class="cell {"on" if on else ""}{today_cls}" title="{day}">'
                    f"{'●' if on else '○'}</span>"
                )
            pct = round(
                100
                * sum(
                    1 for i in range(7) if (h["id"], (date.today() - timedelta(days=i)).isoformat()) in have
                )
                / 7
            )
            blocks.append(
                f'<div class="habit"><div class="hhead"><b>{esc(h["name"])}</b>'
                f'<span class="muted">goal {h["target"]}/wk · {pct}% this week · {streak}-day run</span>'
                f'<form method="post" action="/habits/{h["id"]}/toggle" class="inline">'
                f'<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">'
                f'<button class="small">{"Unmark today" if (h["id"], _today()) in have else "Mark today"}</button></form>'
                f'<form method="post" action="/habits/{h["id"]}/delete" class="inline">'
                f'<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">'
                f'<button class="small danger">Drop</button></form></div>'
                f'<div class="cells">{"".join(cells)}</div></div>'
            )
        body = f"""<div class="panel"><form method="post" action="/habits" class="form inline-form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<input name="name" required maxlength="80" placeholder="New habit — e.g. Sweep the shop">
<select name="target">{"".join(f'<option value="{i}">{i}/week</option>' for i in (2, 3, 5, 7))}</select>
<button class="small">Start it</button></form></div>
<div class="panel">{"".join(blocks) or '<p class="muted">No habits. The bench stays dusty.</p>'}</div>"""
        return layout("Habits", body, "habits", req=req, email=me["email"])

    @app.post("/habits")
    async def habit_create(req, uid=Depends(current_user)):
        form = await req.form()
        try:
            data = HabitIn.validate({"name": form.get("name", ""), "target": form.get("target", "5")})
        except Exception as e:
            raise BadRequest(f"fix the habit and retry: {e}")
        await req.app.state_db.execute(
            "INSERT INTO habits (user_id, name, target, created) VALUES (?, ?, ?, ?)",
            uid,
            data.name,
            int(data.target),
            _now(),
        )
        await CACHE.delete(f"dash:{uid}")
        return RedirectResponse("/habits", status_code=303)

    @app.post("/habits/{hid:int}/toggle")
    async def habit_toggle(req, hid: int, uid=Depends(current_user)):
        row = await req.app.state_db.fetch_one("SELECT * FROM habits WHERE id = ? AND user_id = ?", hid, uid)
        if not row:
            raise NotFound(f"no habit #{hid} on your list")
        today = _today()
        hit = await req.app.state_db.fetch_one(
            "SELECT id FROM habit_checks WHERE habit_id = ? AND day = ?", hid, today
        )
        if hit:
            await req.app.state_db.execute("DELETE FROM habit_checks WHERE id = ?", hit["id"])
        else:
            await req.app.state_db.execute(
                "INSERT INTO habit_checks (habit_id, day) VALUES (?, ?)", hid, today
            )
        await CACHE.delete(f"dash:{uid}")
        return RedirectResponse("/habits", status_code=303)

    @app.post("/habits/{hid:int}/delete")
    async def habit_delete(req, hid: int, uid=Depends(current_user)):
        await req.app.state_db.execute("DELETE FROM habit_checks WHERE habit_id = ?", hid)
        await req.app.state_db.execute("DELETE FROM habits WHERE id = ? AND user_id = ?", hid, uid)
        await CACHE.delete(f"dash:{uid}")
        return RedirectResponse("/habits", status_code=303)

    # ---- crew + settings + search ----

    @app.get("/team")
    async def team_page(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        members = await req.app.state_db.fetch_all(
            "SELECT name, email, role, created FROM users ORDER BY created"
        )
        rows = "".join(
            f"<tr><td><b>{esc(m['name'])}</b></td><td>{esc(m['email'])}</td>"
            f'<td><span class="pill">{esc(m["role"])}</span></td><td class="d">{esc(m["created"][:10])}</td></tr>'
            for m in members
        )
        body = f"""<div class="panel"><h2>Crew · {len(members)}</h2>
<table>{rows}</table></div>
<div class="panel"><h2>Hand out a bench</h2>
<form method="post" action="/team/invite" class="form inline-form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<input name="email" type="email" required placeholder="teammate@example.com">
<button class="small">Queue invite</button></form>
<p class="muted">Invites go out as durable queue jobs — <code>ikarem worker forge.app:app</code> delivers them.</p></div>"""
        return layout("Crew", body, "team", req=req, email=me["email"])

    @app.post("/team/invite")
    async def team_invite(req, uid=Depends(current_user)):
        form = await req.form()
        email = (form.get("email", "") or "").strip()[:254]
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            raise BadRequest("that email will not reach anyone — check it and retry")
        try:
            if hasattr(req.app, "state_queue"):
                await req.app.state_queue.enqueue("forge-welcome", {"to": email})
        except Exception:
            pass
        await log(req.app.state_db, uid, "invite", f"Invite queued for {email}.")
        flash(req, f"Invite for {email} is in the queue.")
        return RedirectResponse("/team", status_code=303)

    @app.get("/settings")
    async def settings_page(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        keys = await req.app.state_db.fetch_all(
            "SELECT id, name, prefix, created FROM api_keys WHERE user_id = ? ORDER BY id DESC", uid
        )
        rows = (
            "".join(
                f"<li><b>{esc(k['name'])}</b> <code>{esc(k['prefix'])}…</code>"
                f' <span class="muted">{esc(k["created"])}</span>'
                f'<form method="post" action="/settings/keys/{k["id"]}/revoke" class="inline">'
                f'<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">'
                f'<button class="mini danger">revoke</button></form></li>'
                for k in keys
            )
            or '<li class="muted">No keys cut.</li>'
        )
        shown = req.session.pop("new_token", None)
        show = (
            f'<p class="tok">Fresh key — copy it now, it will not show again:<br><code>{esc(shown)}</code></p>'
            if shown
            else ""
        )
        body = f"""<div class="panel"><h2>Bench</h2>
<p>Signed in as <b>{esc(me["name"])}</b> · {esc(me["email"])} · {esc(me["role"])}</p>
<form method="post" action="/settings/token" class="inline">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<button class="small">Mint a day-pass JWT</button></form>
<p class="muted">Day-passes ride as <code>Authorization: Bearer …</code> for scripts.</p></div>
<div class="panel"><h2>API keys</h2>{show}<ul class="plain">{rows}</ul>
<form method="post" action="/settings/keys" class="form inline-form" style="margin-top:10px">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<input name="name" maxlength="60" placeholder="Key name — e.g. label printer">
<button class="small">Cut a key</button></form>
<p class="muted">Keys ride as <code>X-API-Key</code>. The shop keeps only a hash.</p></div>
<div class="panel"><h2>Exports</h2>
<p class="rowbtns"><a class="btn ghost" href="/export/expenses.csv">Ledger CSV</a>
<a class="btn ghost" href="/export/tasks.csv">Cards CSV</a></p></div>"""
        return layout("Settings", body, "settings", req=req, email=me["email"])

    @app.post("/settings/token")
    async def mint_token(req, uid=Depends(current_user)):
        from ikarem import create_token as _mint

        tok = _mint(uid, req.app.config.get("auth_secret", "change-me"), expires_in=86400)
        return JSONResponse({"token": tok, "use": "Authorization: Bearer <token>"})

    @app.post("/settings/keys")
    async def key_create(req, uid=Depends(current_user)):
        form = await req.form()
        name = (form.get("name", "") or "unlabeled").strip()[:60] or "unlabeled"
        raw = "fg_" + secrets.token_urlsafe(24)
        await req.app.state_db.execute(
            "INSERT INTO api_keys (user_id, name, prefix, phash, created) VALUES (?, ?, ?, ?, ?)",
            uid,
            name,
            raw[:10],
            hashlib.sha256(raw.encode()).hexdigest(),
            _now(),
        )
        req.session["new_token"] = raw
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/keys/{kid:int}/revoke")
    async def key_revoke(req, kid: int, uid=Depends(current_user)):
        await req.app.state_db.execute("DELETE FROM api_keys WHERE id = ? AND user_id = ?", kid, uid)
        flash(req, "Key revoked.")
        return RedirectResponse("/settings", status_code=303)

    @app.get("/search")
    async def search_page(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        q = req.query.get("q", "").strip()[:80]
        body = ""
        if q:
            like = f"%{q}%"
            db = req.app.state_db
            projs = await db.fetch_all(
                "SELECT id, name FROM projects WHERE name LIKE ? OR brief LIKE ? LIMIT 10", like, like
            )
            tasks = await db.fetch_all("SELECT id, title FROM tasks WHERE title LIKE ? LIMIT 10", like)
            notes = await db.fetch_all(
                "SELECT id, title FROM notes WHERE title LIKE ? OR body LIKE ? LIMIT 10", like, like
            )
            secs = []
            if projs:
                secs.append(
                    "<h2>Jobs</h2>"
                    + "".join(f'<li><a href="/projects/{p["id"]}">{esc(p["name"])}</a></li>' for p in projs)
                )
            if tasks:
                secs.append(
                    "<h2>Cards</h2>"
                    + "".join(
                        f'<li><a href="/tasks?state=">{esc(t["title"])}</a> <span class="muted">#{t["id"]}</span></li>'
                        for t in tasks
                    )
                )
            if notes:
                secs.append(
                    "<h2>Notes</h2>"
                    + "".join(f'<li><a href="/notes/{n["id"]}">{esc(n["title"])}</a></li>' for n in notes)
                )
            body = (
                f'<div class="panel"><ul class="plain">{"".join(secs) or "<li>No matches.</li>"}</ul></div>'
            )
        else:
            body = '<div class="panel"><p class="muted">Type above — jobs, cards, notes.</p></div>'
        return layout(f"Search · {q}" if q else "Search", body, "", req=req, email=me["email"])

    @app.get("/explorer")
    async def explorer_page(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        rows = [
            ("GET", "/api/summary", "Shop numbers — income, spend, balance"),
            ("GET", "/api/projects", "Every job on the floor"),
            ("GET", "/api/projects/1", "One job as a dossier (swap in any id)"),
            ("GET", "/api/projects/1/issues", "Cards on a job, ?state=todo|doing|done"),
            ("POST", "/api/projects/1/issues", "Cut a card — JSON or NISH body"),
            ("GET", "/api/projects/1/activity", "Job timeline, newest first"),
            ("GET", "/api/tasks", "All cards, ?state= filter"),
            ("GET", "/api/notes", "Every pinned note"),
            ("GET", "/api/expenses", "Ledger entries"),
            ("GET", "/api/metrics", "Shop-wide rollup + 14-day rhythm"),
            ("GET", "/api/notifications", "Derived inbox: overdue, habits, stalled"),
            ("GET", "/api/search?q=awning", "Full-shop search"),
            ("GET", "/openapi.json", "Machine contract (JSON)"),
            ("GET", "/openapi.nish", "Machine contract (NISH)"),
        ]
        lines = "".join(
            f'<tr><td><span class="pill">{m}</span></td><td><code>{esc(p)}</code></td>'
            f"<td>{esc(d)}</td>"
            f'<td class="r"><a href="{esc(p)}">JSON</a> · '
            f'<a href="{esc(p)}{"&" if "?" in p else "?"}format=nish">NISH</a></td></tr>'
            for m, p, d in rows
        )
        body = f"""<p class="lede">Every row below answers twice. Take the NISH link with the
Viewer extension installed and it paints a structured tree — no client code.</p>
<div class="panel"><table>{lines}</table></div>
<div class="panel"><h2>Posting NISH</h2>
<p>Cards accept NISH request bodies too. <code>Content-Type: application/x-nish</code>:</p>
<pre>NISH/1.0

title = "Cut pine"
priority = 3
due = "2026-11-01"</pre>
<p class="muted">Malformed bodies fail loud with a 400 naming the line — never silently degraded.</p></div>"""
        return layout("API explorer", body, "explorer", req=req, email=me["email"])


# ---------------------------------------------------------------- JSON api (blueprint)


def _register_api(api: Blueprint) -> None:
    @api.get("/summary")
    async def api_summary(req, uid=Depends(current_user)):
        """Shop numbers as JSON."""
        db = req.app.state_db
        open_n = (await db.fetch_one("SELECT COUNT(*) AS n FROM tasks WHERE state != 'done'"))["n"]
        done_n = (await db.fetch_one("SELECT COUNT(*) AS n FROM tasks WHERE state = 'done'"))["n"]
        sums = await db.fetch_one(
            "SELECT COALESCE(SUM(CASE WHEN kind='income' THEN cents END),0) AS i,"
            " COALESCE(SUM(CASE WHEN kind='expense' THEN cents END),0) AS e FROM expenses"
        )
        return {
            "open_cards": open_n,
            "done_cards": done_n,
            "income_cents": sums["i"],
            "expense_cents": sums["e"],
            "balance_cents": sums["i"] - sums["e"],
        }

    @api.get("/projects")
    async def api_projects(req, uid=Depends(current_user)):
        """List jobs."""
        rows = await req.app.state_db.fetch_all("SELECT * FROM projects ORDER BY id DESC")
        return {"jobs": [dict(r) for r in rows]}

    @api.post("/projects")
    async def api_project_create(req, proj: ProjectIn, uid=Depends(current_user)):
        """Start a job (JSON)."""
        pid = await req.app.state_db.execute(
            "INSERT INTO projects (owner_id, name, brief, status, color, created) VALUES (?, ?, ?, ?, ?, ?)",
            uid,
            proj.name,
            proj.brief,
            "open",
            proj.color,
            _now(),
        )
        await CACHE.delete(f"dash:{uid}")
        return {"id": pid, "name": proj.name}, 201

    @api.get("/tasks")
    async def api_tasks(req, uid=Depends(current_user)):
        """List cards, optional ?state=todo|doing|done."""
        state = req.query.get("state", "")
        if state in BOARD_STATES:
            rows = await req.app.state_db.fetch_all(
                "SELECT * FROM tasks WHERE state = ? ORDER BY id DESC LIMIT 100", state
            )
        else:
            rows = await req.app.state_db.fetch_all("SELECT * FROM tasks ORDER BY id DESC LIMIT 100")
        return {"cards": [dict(r) for r in rows]}

    @api.post("/tasks")
    async def api_task_create(req, card: TaskIn, uid=Depends(current_user)):
        """Cut a card (JSON)."""
        tid = await req.app.state_db.execute(
            "INSERT INTO tasks (project_id, title, detail, state, priority, assignee, due, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            None,
            card.title,
            card.detail,
            "todo",
            int(card.priority),
            "",
            card.due,
            _now(),
        )
        await CACHE.delete(f"dash:{uid}")
        return {"id": tid, "title": card.title}, 201

    @api.get("/notes")
    async def api_notes(req, uid=Depends(current_user)):
        """List notes."""
        rows = await req.app.state_db.fetch_all("SELECT * FROM notes ORDER BY id DESC LIMIT 100")
        return {"notes": [dict(r) for r in rows]}

    @api.post("/notes")
    async def api_note_create(req, note: NoteIn, uid=Depends(current_user)):
        """Pin a note (JSON)."""
        nid = await req.app.state_db.execute(
            "INSERT INTO notes (project_id, author_id, title, body, updated) VALUES (?, ?, ?, ?, ?)",
            None,
            uid,
            note.title,
            note.body,
            _now(),
        )
        return {"id": nid}, 201

    @api.get("/expenses")
    async def api_expenses(req, uid=Depends(current_user)):
        """List ledger entries."""
        rows = await req.app.state_db.fetch_all("SELECT * FROM expenses ORDER BY day DESC, id DESC LIMIT 100")
        return {"entries": [dict(r) for r in rows]}

    @api.post("/expenses")
    async def api_expense_create(req, entry: ExpenseIn, uid=Depends(current_user)):
        """Book an entry (JSON). Idempotent with Idempotency-Key."""
        eid = await req.app.state_db.execute(
            "INSERT INTO expenses (project_id, user_id, day, label, cents, kind, category)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            None,
            uid,
            entry.day or _today(),
            entry.label,
            int(round(float(entry.amount) * 100)),
            entry.kind,
            entry.category,
        )
        await CACHE.delete(f"dash:{uid}")
        return {"id": eid}, 201

    @api.get("/search")
    async def api_search(req, uid=Depends(current_user)):
        """Full-shop search: ?q=… across jobs, cards, notes."""
        q = req.query.get("q", "").strip()[:80]
        if not q:
            raise BadRequest("pass ?q=… — e.g. /api/search?q=awning")
        like = f"%{q}%"
        db = req.app.state_db
        return {
            "q": q,
            "jobs": [
                dict(r)
                for r in await db.fetch_all("SELECT id, name FROM projects WHERE name LIKE ? LIMIT 10", like)
            ],
            "cards": [
                dict(r)
                for r in await db.fetch_all("SELECT id, title FROM tasks WHERE title LIKE ? LIMIT 10", like)
            ],
            "notes": [
                dict(r)
                for r in await db.fetch_all("SELECT id, title FROM notes WHERE title LIKE ? LIMIT 10", like)
            ],
        }

    @api.get("/projects/{pid:int}")
    async def api_project_dossier(req, pid: int, uid=Depends(current_user)):
        """One job as a dossier — shaped for the NISH viewer (?format=nish)."""
        db = req.app.state_db
        p = await db.fetch_one("SELECT * FROM projects WHERE id = ?", pid)
        if not p:
            raise NotFound(f"no job #{pid} — see /api/projects for the floor")
        pid = p["id"]
        states = {s: 0 for s in BOARD_STATES}
        for r in await db.fetch_all(
            "SELECT state, COUNT(*) AS n FROM tasks WHERE project_id = ? GROUP BY state", pid
        ):
            states[r["state"]] = r["n"]
        notes_n = (await db.fetch_one("SELECT COUNT(*) AS n FROM notes WHERE project_id = ?", pid))["n"]
        files_n = (await db.fetch_one("SELECT COUNT(*) AS n FROM files WHERE project_id = ?", pid))["n"]
        remarks_n = (await db.fetch_one("SELECT COUNT(*) AS n FROM comments WHERE project_id = ?", pid))["n"]
        spend = await db.fetch_one(
            "SELECT COALESCE(SUM(CASE WHEN kind='income' THEN cents END),0) AS i,"
            " COALESCE(SUM(CASE WHEN kind='expense' THEN cents END),0) AS e"
            " FROM expenses WHERE project_id = ?",
            pid,
        )
        return negotiate(
            req,
            {
                "id": pid,
                "name": p["name"],
                "brief": p["brief"] or "",
                "status": p["status"],
                "color": p["color"],
                "cards": states,
                "shelves": {"notes": notes_n, "files": files_n, "remarks": remarks_n},
                "earned_cents": spend["i"],
                "spent_cents": spend["e"],
            },
        )

    @api.get("/projects/{pid:int}/issues")
    async def api_project_issues(req, pid: int, uid=Depends(current_user)):
        """Cards on a job ("issues" in viewer-speak). ?state=todo|doing|done."""
        db = req.app.state_db
        if await db.fetch_one("SELECT id FROM projects WHERE id = ?", pid) is None:
            raise NotFound(f"no job #{pid} — see /api/projects for the floor")
        state = req.query.get("state", "")
        if state in BOARD_STATES:
            rows = await db.fetch_all(
                "SELECT id, title, detail, state, priority, assignee, due"
                " FROM tasks WHERE project_id = ? AND state = ? ORDER BY priority DESC, id",
                pid,
                state,
            )
        else:
            rows = await db.fetch_all(
                "SELECT id, title, detail, state, priority, assignee, due"
                " FROM tasks WHERE project_id = ? ORDER BY priority DESC, id",
                pid,
            )
        return negotiate(
            req,
            {
                "job": pid,
                "state": state or "all",
                "total": len(rows),
                "issues": [dict(r) for r in rows],
            },
        )

    @api.post("/projects/{pid:int}/issues")
    async def api_issue_create(req, pid: int, uid=Depends(current_user)):
        """Cut a card on a job. Speaks JSON *and* NISH request bodies."""
        db = req.app.state_db
        if await db.fetch_one("SELECT id FROM projects WHERE id = ?", pid) is None:
            raise NotFound(f"no job #{pid} — see /api/projects for the floor")
        if "nish" in req.headers.get("content-type", "").lower():
            raw = await req.nish()  # BadRequest naming the line when malformed
            if not isinstance(raw, dict):
                raise BadRequest('NISH card bodies are maps — e.g. title = "Cut pine"')
            payload = {
                "title": raw.get("title", ""),
                "detail": raw.get("detail", ""),
                "priority": raw.get("priority", 2),
                "due": raw.get("due", ""),
            }
        else:
            try:
                payload = await req.json()
            except Exception:
                raise BadRequest('send a JSON object or a NISH map — e.g. {"title": "Cut pine"}')
        try:
            data = TaskIn.validate(payload)
        except Exception as e:
            raise BadRequest(f"fix the card and retry: {e}")
        tid = await db.execute(
            "INSERT INTO tasks (project_id, title, detail, state, priority, assignee, due, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            pid,
            data.title,
            data.detail,
            "todo",
            int(data.priority),
            "",
            data.due,
            _now(),
        )
        await CACHE.delete(f"dash:{uid}")
        await log(db, uid, "task", f"Card #{tid} cut on job #{pid}: “{data.title}”.")
        return negotiate(req, {"id": tid, "job": pid, "title": data.title, "state": "todo"}, 201)

    @api.get("/projects/{pid:int}/activity")
    async def api_project_activity(req, pid: int, uid=Depends(current_user)):
        """Job timeline, newest first — cards, notes, remarks, chat, files, money."""
        db = req.app.state_db
        if await db.fetch_one("SELECT id FROM projects WHERE id = ?", pid) is None:
            raise NotFound(f"no job #{pid} — see /api/projects for the floor")
        events: list[dict] = []
        for t in await db.fetch_all("SELECT id, title, state, created FROM tasks WHERE project_id = ?", pid):
            events.append(
                {"at": t["created"], "kind": "card", "text": f"#{t['id']} {t['title']} [{t['state']}]"}
            )
        for n in await db.fetch_all("SELECT title, updated FROM notes WHERE project_id = ?", pid):
            events.append({"at": n["updated"], "kind": "note", "text": f"Note: {n['title']}"})
        for cm in await db.fetch_all("SELECT name, body, created FROM comments WHERE project_id = ?", pid):
            events.append({"at": cm["created"], "kind": "remark", "text": f"{cm['name']}: {cm['body']}"})
        for ch in await db.fetch_all("SELECT name, body, created FROM chat WHERE project_id = ?", pid):
            events.append({"at": ch["created"], "kind": "chat", "text": f"{ch['name']}: {ch['body']}"})
        for f in await db.fetch_all("SELECT name, created FROM files WHERE project_id = ?", pid):
            events.append({"at": f["created"], "kind": "file", "text": f"Shelved: {f['name']}"})
        for e in await db.fetch_all("SELECT label, cents, kind, day FROM expenses WHERE project_id = ?", pid):
            sign = "+" if e["kind"] == "income" else "-"
            events.append(
                {"at": e["day"], "kind": "money", "text": f"{sign}{money(e['cents'])} {e['label']}"}
            )
        events.sort(key=lambda ev: ev["at"] or "", reverse=True)
        return negotiate(req, {"job": pid, "total": len(events), "events": events[:40]})

    @api.get("/metrics")
    async def api_metrics(req, uid=Depends(current_user)):
        """Shop-wide numbers + per-job rollup + 14-day rhythm. Viewer-ready."""
        db = req.app.state_db
        open_n = (await db.fetch_one("SELECT COUNT(*) AS n FROM tasks WHERE state != 'done'"))["n"]
        done_n = (await db.fetch_one("SELECT COUNT(*) AS n FROM tasks WHERE state = 'done'"))["n"]
        jobs_n = (await db.fetch_one("SELECT COUNT(*) AS n FROM projects"))["n"]
        crew_n = (await db.fetch_one("SELECT COUNT(*) AS n FROM users"))["n"]
        sums = await db.fetch_one(
            "SELECT COALESCE(SUM(CASE WHEN kind='income' THEN cents END),0) AS i,"
            " COALESCE(SUM(CASE WHEN kind='expense' THEN cents END),0) AS e FROM expenses"
        )
        rollup = await db.fetch_all(
            "SELECT p.id, p.name, p.status,"
            " (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.state != 'done') AS open_cards,"
            " (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.state = 'done') AS done_cards,"
            " COALESCE((SELECT SUM(cents) FROM expenses e"
            " WHERE e.project_id = p.id AND e.kind = 'expense'),0) AS spent_cents"
            " FROM projects p ORDER BY p.id"
        )
        rhythm = []
        for d in range(13, -1, -1):
            day = (date.today() - timedelta(days=d)).isoformat()
            n = (await db.fetch_one("SELECT COUNT(*) AS n FROM habit_checks WHERE day = ?", day))["n"]
            rhythm.append({"day": day, "checks": n})
        return negotiate(
            req,
            {
                "open_cards": open_n,
                "done_cards": done_n,
                "jobs": jobs_n,
                "crew": crew_n,
                "income_cents": sums["i"],
                "expense_cents": sums["e"],
                "projects": [dict(r) for r in rollup],
                "rhythm": rhythm,
            },
        )

    @api.get("/notifications")
    async def api_notifications(req, uid=Depends(current_user)):
        """Derived inbox: overdue, due-soon, slow habits, stalled jobs."""
        db = req.app.state_db
        today = _today()
        soon = (date.today() + timedelta(days=2)).isoformat()
        items: list[dict] = []
        for t in await db.fetch_all(
            "SELECT t.id, t.title, t.due, t.project_id FROM tasks t"
            " WHERE t.state != 'done' AND t.due != '' AND t.due < ? ORDER BY t.due LIMIT 20",
            today,
        ):
            href = f"/projects/{t['project_id']}/board" if t["project_id"] else "/tasks"
            items.append(
                {
                    "kind": "overdue",
                    "text": f"Card #{t['id']} “{t['title']}” was due {t['due']}",
                    "href": href,
                }
            )
        for t in await db.fetch_all(
            "SELECT t.id, t.title, t.due, t.project_id FROM tasks t"
            " WHERE t.state != 'done' AND t.due >= ? AND t.due <= ? ORDER BY t.due LIMIT 20",
            today,
            soon,
        ):
            href = f"/projects/{t['project_id']}/board" if t["project_id"] else "/tasks"
            items.append(
                {"kind": "due_soon", "text": f"Card #{t['id']} “{t['title']}” due {t['due']}", "href": href}
            )
        for h in await db.fetch_all("SELECT * FROM habits WHERE user_id = ?", uid):
            n = (
                await db.fetch_one(
                    "SELECT COUNT(*) AS n FROM habit_checks WHERE habit_id = ? AND day >= ?",
                    h["id"],
                    (date.today() - timedelta(days=6)).isoformat(),
                )
            )["n"]
            if n < h["target"]:
                items.append(
                    {
                        "kind": "habit",
                        "text": f"“{h['name']}” at {n}/{h['target']} this week",
                        "href": "/habits",
                    }
                )
        for p in await db.fetch_all(
            "SELECT p.id, p.name FROM projects p WHERE p.status = 'open'"
            " AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.project_id = p.id AND t.state = 'done')"
            " AND p.created < ?",
            (datetime.now() - timedelta(days=30)).isoformat(),
        ):
            items.append(
                {
                    "kind": "stalled",
                    "text": f"Job “{p['name']}” — nothing finished in a month",
                    "href": f"/projects/{p['id']}",
                }
            )
        return negotiate(req, {"today": today, "total": len(items), "items": items})


# ---------------------------------------------------------------- method view + websocket


class ProjectResource(MethodView):
    """One class per resource: full DI per method."""

    async def get(self, req, pid: int):
        row = await req.app.state_db.fetch_one("SELECT * FROM projects WHERE id = ?", pid)
        if not row:
            raise NotFound(f"no job #{pid}")
        return dict(row)

    async def delete(self, req, pid: int):
        await req.app.state_db.execute("DELETE FROM projects WHERE id = ?", pid)
        return {"ok": True, "id": pid}


def _register_ws(app: Ikarem) -> None:
    app.route("/jobs/{pid:int}", ["GET", "DELETE"])(ProjectResource.as_view("job_resource"))

    @app.websocket("/ws/hall")
    async def hall(ws: WebSocket):
        await ws.accept()
        try:
            first = json.loads(await ws.receive_text() or "{}")
        except WebSocketDisconnect:
            return
        pid = str(first.get("project", "lobby"))
        name = str(first.get("name", "anon"))[:40] or "anon"
        room = ROOMS.setdefault(pid, Room())
        await room.join(ws)
        db = getattr(getattr(ws, "app", None), "state_db", None)
        await room.broadcast({"sys": f"{name} walked in (job {pid})."}, exclude=ws)
        try:
            while True:
                text = await ws.receive_text()
                try:
                    payload = json.loads(text)
                    body = str(payload.get("body", text))[:2000]
                except Exception:
                    body = text[:2000]
                if db is not None and pid.isdigit():
                    try:
                        await db.execute(
                            "INSERT INTO chat (project_id, user_id, name, body, created)"
                            " VALUES (?, ?, ?, ?, ?)",
                            int(pid),
                            "ws",
                            name,
                            body,
                            _now(),
                        )
                    except Exception:
                        pass
                await room.broadcast({"name": name, "body": body})
        except WebSocketDisconnect:
            pass
        finally:
            room.leave(ws)


app = create_app()
