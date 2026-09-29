"""Cadence — small habits, kept daily.

A habit tracker on IKAREM: session auth + CSRF-protected HTML forms,
a real streak engine, heatmaps, CSV export, and a JSON API (see /docs).

Run:  uvicorn cadence.app:app
Test: python -m pytest cadence/tests -q
"""

import os
import re
import secrets
from datetime import date, timedelta
from pathlib import Path

from ikarem import (
    BackgroundTasks,
    BadRequest,
    CORSMiddleware,
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
    Unauthorized,
    check_password,
    csrf_token,
    flash,
    get_flashed_messages,
    hash_password,
    verify_token,
)
from ikarem.db import DatabasePlugin
from ikarem.http import HTMLResponse, RedirectResponse, StreamingResponse

BASE = Path(__file__).parent

SESSION_SECRET = os.environ.get("IKAREM_SESSION_SECRET", "cadence-dev-secret-change-in-prod")
AUTH_SECRET = os.environ.get("IKAREM_AUTH_SECRET", "cadence-dev-auth-change-in-prod")
DB_URL = os.environ.get("IKAREM_DB_URL", f"sqlite:///{BASE / 'cadence.db'}")

app = Ikarem(
    session_secret=SESSION_SECRET,
    auth_secret=AUTH_SECRET,
    db_url=DB_URL,
    version="1.0.0",
)
app.use(RequestIDMiddleware())
app.use(SecurityHeadersMiddleware())
app.use(CORSMiddleware())
app.use(SessionMiddleware())
app.use(CSRFMiddleware())
app.use(RateLimitMiddleware(per_minute=240))
app.register(DatabasePlugin(app.config.get("db_url", DB_URL)))
app.mount_static("/static", str(BASE / "static"))

# ----------------------------------------------------------------------------
# Domain constants
# ----------------------------------------------------------------------------

PALETTE = {
    "pine": "#1F6B4D",
    "moss": "#4D7C0F",
    "teal": "#0E7490",
    "blue": "#3B5BD6",
    "plum": "#8B3FA0",
    "rust": "#C2410C",
    "clay": "#B45309",
    "rose": "#BE123C",
}
COLOR_PATTERN = r"^(pine|moss|teal|blue|plum|rust|clay|rose)$"
DAY_PATTERN = r"^(\d{4}-\d{2}-\d{2})?$"
HEATMAP_WEEKS = 12


@app.on_startup
async def init_db():
    db = app.state_db
    await db.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE, pw TEXT)")
    await db.execute(
        "CREATE TABLE IF NOT EXISTS habits (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT,"
        " name TEXT, description TEXT DEFAULT '', color TEXT DEFAULT 'pine',"
        " target_per_week INTEGER DEFAULT 7, archived INTEGER DEFAULT 0, created_at TEXT)"
    )
    if getattr(db, "dialect", "sqlite") == "postgres":
        await db.execute(
            "CREATE TABLE IF NOT EXISTS checkins (id SERIAL PRIMARY KEY, habit_id INTEGER,"
            " day TEXT, note TEXT DEFAULT '', UNIQUE(habit_id, day))"
        )
    else:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS checkins (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " habit_id INTEGER, day TEXT, note TEXT DEFAULT '', UNIQUE(habit_id, day))"
        )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_habits_user ON habits(user_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_checkins_habit_day ON checkins(habit_id, day)")


# ----------------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------------


class Signup(Schema):
    email: str = Field(..., email=True, max_length=254)
    password: str = Field(..., min_length=8, max_length=128)


class HabitIn(Schema):
    name: str = Field(..., min_length=1, max_length=60)
    description: str = Field("", max_length=280)
    color: str = Field("pine", pattern=COLOR_PATTERN)
    target_per_week: int = Field(7, ge=1, le=7, description="Sessions aimed for per week, 1-7")


class ToggleIn(Schema):
    day: str = Field("", pattern=DAY_PATTERN, description="YYYY-MM-DD, defaults to today")


# ----------------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------------


class LoginRequired(Unauthorized):
    """Anonymous visitor: browsers redirect to /login, APIs get JSON 401."""


@app.exception_handler(LoginRequired)
async def login_required_handler(req, exc):
    if wants_html(req):
        return RedirectResponse("/login", status_code=303)
    from ikarem.http import JSONResponse

    return JSONResponse({"detail": "login required"}, status_code=401)


def current_user(req):
    uid = req.session.get("uid")
    if uid:
        return uid
    auth = req.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        try:
            return verify_token(auth[7:], req.app.config.get("auth_secret", "change-me"))["sub"]
        except Exception:
            pass
    raise LoginRequired("login required")


def wants_html(req) -> bool:
    return "text/html" in req.headers.get("accept", "")


async def _user_email(req, uid: str) -> str:
    row = await req.app.state_db.fetch_one("SELECT email FROM users WHERE id = ?", uid)
    return row["email"] if row else "?"


# ----------------------------------------------------------------------------
# Streak engine (pure functions — unit-tested)
# ----------------------------------------------------------------------------


def parse_day(s: str) -> date:
    return date.fromisoformat(s)


def current_streak(done: set, today: date) -> int:
    """Consecutive days ending today — or yesterday if today isn't done yet."""
    n = 0
    d = today
    if d.isoformat() not in done:
        d -= timedelta(days=1)
    while d.isoformat() in done:
        n += 1
        d -= timedelta(days=1)
    return n


def longest_streak(done: set) -> int:
    """Longest unbroken run, ever."""
    if not done:
        return 0
    days = sorted(done)
    best = run = 1
    prev = parse_day(days[0])
    for s in days[1:]:
        d = parse_day(s)
        if d == prev + timedelta(days=1):
            run += 1
        else:
            best = max(best, run)
            run = 1
        prev = d
    return max(best, run)


def week_start(today: date) -> date:
    return today - timedelta(days=today.weekday())


def habit_stats(done: set, today: date, target: int) -> dict:
    wk = week_start(today)
    week_days = {(wk + timedelta(days=i)).isoformat() for i in range(7)}
    week_done = len(done & week_days)
    cutoff = (today - timedelta(days=29)).isoformat()
    last30 = sum(1 for d in done if d >= cutoff)
    return {
        "current": current_streak(done, today),
        "longest": longest_streak(done),
        "week_done": week_done,
        "week_target": target,
        "last30_done": last30,
        "last30_rate": round(last30 / 30, 3),
        "done_today": today.isoformat() in done,
    }


def normalize_day(raw: str | None, today: date) -> str:
    """Validate a YYYY-MM-DD string (or default to today). Rejects the future."""
    s = (raw or "").strip() or today.isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise BadRequest("day must be YYYY-MM-DD")
    try:
        d = parse_day(s)
    except ValueError:
        raise BadRequest("day is not a real calendar date")
    if d > today:
        raise BadRequest("can't log the future")
    return s


def safe_next(raw: str | None) -> str:
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


# ----------------------------------------------------------------------------
# Data access
# ----------------------------------------------------------------------------


async def _habits_for(db, uid: str, include_archived: bool = False):
    q = "SELECT * FROM habits WHERE user_id = ?"
    if not include_archived:
        q += " AND archived = 0"
    return await db.fetch_all(q + " ORDER BY created_at, id", uid)


async def _get_habit(db, hid: int, uid: str):
    row = await db.fetch_one("SELECT * FROM habits WHERE id = ? AND user_id = ?", hid, uid)
    if not row:
        raise NotFound("habit not found")
    return row


async def _days_by_habit(db, uid: str) -> dict:
    rows = await db.fetch_all(
        "SELECT c.habit_id AS hid, c.day AS day FROM checkins c"
        " JOIN habits h ON h.id = c.habit_id WHERE h.user_id = ?",
        uid,
    )
    out: dict = {}
    for r in rows:
        out.setdefault(r["hid"], set()).add(r["day"])
    return out


# ----------------------------------------------------------------------------
# HTML helpers
# ----------------------------------------------------------------------------


def esc(s: object) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def layout(
    title: str, body: str, active: str = "", email: str | None = None, csrf: str = "", req=None
) -> HTMLResponse:
    nav = "".join(
        f'<a href="{href}" class="{"on" if active == key else ""}">{label}</a>'
        for key, href, label in [
            ("today", "/", "Today"),
            ("habits", "/habits", "Habits"),
            ("export", "/export.csv", "Export"),
            ("docs", "/docs", "API"),
        ]
    )
    user = (
        f'<span class="who">{esc(email)}</span>'
        f'<form method="post" action="/logout" class="inline">'
        f'<input type="hidden" name="_csrf_token" value="{csrf}">'
        f'<button class="ghost">Log out</button></form>'
        if email
        else '<a class="login-link" href="/login">Log in</a>'
    )
    flashes = ""
    if req is not None:
        msgs = get_flashed_messages(req)
        if msgs:
            flashes = (
                '<div class="flashes">' + "".join(f'<p class="flash">{esc(m)}</p>' for m in msgs) + "</div>"
            )
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="csrf-token" content="{csrf}">
<title>{esc(title)} · Cadence</title>
<link rel="stylesheet" href="/static/style.css">
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml"></head>
<body><header class="topbar"><div class="wrap bar">
<a class="brand" href="/"><span class="mark" aria-hidden="true"></span>Cadence</a>
<nav>{nav}</nav><div class="user">{user}</div></div></header>
<main class="wrap"><h1>{esc(title)}</h1>{flashes}{body}</main>
<footer class="wrap foot"><span>Small habits, kept daily.</span></footer>
<script src="/static/app.js" defer></script></body></html>"""
    )


def week_dots(done: set, today: date, color: str) -> str:
    """Mon–Sun strip for the current week."""
    wk = week_start(today)
    cells = []
    for i in range(7):
        d = wk + timedelta(days=i)
        iso = d.isoformat()
        cls = "done" if iso in done else ("today" if d == today else "miss")
        if d > today:
            cls = "future"
        label = d.strftime("%a")[0]
        cells.append(f'<span class="wdot {cls}" title="{iso}" style="--hc:{color}">{label}</span>')
    return '<div class="week">' + "".join(cells) + "</div>"


def heatmap(
    done: set, today: date, color: str, weeks: int = HEATMAP_WEEKS, levels: dict | None = None
) -> str:
    """GitHub-style grid, chronological, columns = weeks, rows = Mon..Sun."""
    total = weeks * 7
    end = today
    start = end - timedelta(days=total - 1)
    pad = start.weekday()  # back up to Monday so columns align
    start -= timedelta(days=pad)
    cells = []
    d = start
    while d <= end:
        iso = d.isoformat()
        if d < end - timedelta(days=total - 1):
            cells.append('<span class="hcell pad"></span>')
        elif levels is not None:
            n = levels.get(iso, 0)
            lv = 0 if n == 0 else min(4, 1 + int(n * 3 / max(1, max(levels.values()))))
            cells.append(f'<span class="hcell lv{lv}" title="{iso}: {n} done"></span>')
        else:
            on = " on" if iso in done else ""
            cells.append(f'<span class="hcell{on}" title="{iso}" style="--hc:{color}"></span>')
        d += timedelta(days=1)
    return '<div class="heat" role="img" aria-label="activity heatmap">' + "".join(cells) + "</div>"


def streak_badge(n: int) -> str:
    if n >= 2:
        return f'<span class="streak">{n}-day streak</span>'
    if n == 1:
        return '<span class="streak fresh">day one</span>'
    return '<span class="streak none">not started</span>'


# ----------------------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------------------


@app.get("/")
async def dashboard(req, uid=Depends(current_user)):
    db = req.app.state_db
    today = date.today()
    email = await _user_email(req, uid)
    habits = await _habits_for(db, uid)
    by_habit = await _days_by_habit(db, uid)

    if not habits:
        body = (
            '<div class="panel empty"><h2>Nothing to track yet</h2>'
            "<p>Cadence works best with one to five daily habits. "
            "Name the first one — we'll handle streaks, reminders of progress, and the math.</p>"
            '<p><a class="btn" href="/habits/new">Create your first habit</a></p></div>'
        )
        return layout("Today", body, "today", email, csrf_token(req), req=req)

    cards = []
    done_count = 0
    for h in habits:
        done = by_habit.get(h["id"], set())
        st = habit_stats(done, today, h["target_per_week"])
        done_count += 1 if st["done_today"] else 0
        color = PALETTE.get(h["color"], PALETTE["pine"])
        state = "done" if st["done_today"] else "todo"
        btn_label = "Done" if st["done_today"] else "Mark done"
        cards.append(
            f'<li class="today-row {state}">'
            f'<form method="post" action="/check" class="toggle-form" data-habit="{h["id"]}" data-day="{today.isoformat()}">'
            f'<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">'
            f'<input type="hidden" name="habit_id" value="{h["id"]}">'
            f'<input type="hidden" name="day" value="{today.isoformat()}">'
            f'<button class="toggle" aria-label="{btn_label}: {esc(h["name"])}" title="{btn_label}">'
            f'<svg viewBox="0 0 16 16"><path d="M3 8.5l3.2 3L13 4.5"/></svg></button></form>'
            f'<div class="tmain"><div class="tname">'
            f'<i class="dot" style="background:{color}"></i>'
            f'<a href="/habits/{h["id"]}">{esc(h["name"])}</a>{streak_badge(st["current"])}</div>'
            f"{week_dots(done, today, color)}"
            f'<div class="tsub">{st["week_done"]}/{st["week_target"]} this week · '
            f"best {st['longest']}</div></div></li>"
        )
    # Undone habits first — the list is a to-do list, not a trophy shelf.
    cards.sort(key=lambda c: (">today-row done" in c, c))
    pct = round(done_count / len(habits) * 100) if habits else 0

    levels: dict = {}
    for hid, days in by_habit.items():
        for d in days:
            levels[d] = levels.get(d, 0) + 1
    month = f"{today.strftime('%A, %B')} {today.day}"

    body = (
        f'<p class="dateline">{esc(month)}</p>'
        f'<div class="progress"><div class="pbar"><i style="width:{pct}%"></i></div>'
        f"<span>{done_count} of {len(habits)} done · {pct}%</span></div>"
        f'<ul class="today">{"".join(cards)}</ul>'
        f'<div class="panel"><h2>Last {HEATMAP_WEEKS} weeks</h2>'
        f"{heatmap(set(), today, '', levels=levels)}"
        '<p class="muted small">Every square is a day; darker means more habits kept.</p></div>'
    )
    return layout("Today", body, "today", email, csrf_token(req), req=req)


# ----------------------------------------------------------------------------
# Habit pages
# ----------------------------------------------------------------------------


def _habit_form(req, action: str, d: dict | None = None, err: str = "", submit: str = "Create habit") -> str:
    d = d or {"name": "", "description": "", "color": "pine", "target_per_week": 7}
    try:
        target = int(d.get("target_per_week", 7))
    except (TypeError, ValueError):
        target = 7
    swatches = "".join(
        f'<label class="sw" title="{name}"><input type="radio" name="color" value="{name}"'
        f"{' checked' if d.get('color', 'pine') == name else ''}>"
        f'<i style="background:{hex_}"></i></label>'
        for name, hex_ in PALETTE.items()
    )
    options = "".join(
        f'<option value="{i}"{" selected" if target == i else ""}>{i}× / week</option>' for i in range(1, 8)
    )
    errh = f'<p class="err">{esc(err)}</p>' if err else ""
    return (
        f'{errh}<form method="post" action="{action}" class="form">'
        f'<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">'
        f'<label>Habit<input name="name" value="{esc(d.get("name", ""))}" '
        f'maxlength="60" required placeholder="e.g. Read 20 pages"></label>'
        f'<label class="hint">Why it matters <small>(optional, shown on the detail page)</small>'
        f'<input name="description" value="{esc(d.get("description", ""))}" '
        f'maxlength="280" placeholder="Small steps compound."></label>'
        f'<div class="row"><label>Weekly target<select name="target_per_week">{options}</select></label>'
        f"<div><span class='flabel'>Colour</span>"
        f'<div class="swatches">{swatches}</div></div></div>'
        f"<button>{esc(submit)}</button></form>"
    )


@app.get("/habits")
async def habits_page(req, uid=Depends(current_user)):
    db = req.app.state_db
    today = date.today()
    email = await _user_email(req, uid)
    rows = await _habits_for(db, uid, include_archived=True)
    by_habit = await _days_by_habit(db, uid)
    csrf = csrf_token(req)

    def row(h):
        done = by_habit.get(h["id"], set())
        st = habit_stats(done, today, h["target_per_week"])
        color = PALETTE.get(h["color"], PALETTE["pine"])
        if h["archived"]:
            action = (
                f'<form method="post" action="/habits/{h["id"]}/archive" class="inline">'
                f'<input type="hidden" name="_csrf_token" value="{csrf}">'
                f'<button class="ghost">Restore</button></form>'
            )
            tag = '<span class="arch">archived</span>'
        else:
            action = (
                f'<a class="mini" href="/habits/{h["id"]}/edit">edit</a> · '
                f'<form method="post" action="/habits/{h["id"]}/archive" class="inline">'
                f'<input type="hidden" name="_csrf_token" value="{csrf}">'
                f'<button class="linklike">archive</button></form>'
            )
            tag = streak_badge(st["current"])
        return (
            f'<li class="hrow"><i class="dot" style="background:{color}"></i>'
            f'<a href="/habits/{h["id"]}">{esc(h["name"])}</a>{tag}'
            f'<span class="spacer"></span><span class="muted">{st["last30_done"]}/30 days</span>'
            f"{action}</li>"
        )

    live = [h for h in rows if not h["archived"]]
    done_arch = [h for h in rows if h["archived"]]
    body = (
        '<div class="panel"><div class="panel-head"><h2>Habits</h2>'
        '<a class="btn small" href="/habits/new">+ New habit</a></div>'
        + (
            f'<ul class="hlist">{"".join(row(h) for h in live)}</ul>'
            if live
            else '<p class="muted">No habits yet. <a href="/habits/new">Create one</a>.</p>'
        )
        + "</div>"
    )
    if done_arch:
        body += (
            '<div class="panel"><h2>Archived</h2>'
            f'<ul class="hlist">{"".join(row(h) for h in done_arch)}</ul></div>'
        )
    return layout("Habits", body, "habits", email, csrf, req=req)


@app.get("/habits/new")
async def habit_new(req, uid=Depends(current_user)):
    return layout(
        "New habit",
        f'<div class="panel narrow">{_habit_form(req, "/habits")}</div>',
        "habits",
        await _user_email(req, uid),
        csrf_token(req),
        req=req,
    )


@app.post("/habits")
async def habit_create(req, uid=Depends(current_user)):
    form = await req.form()
    payload = {
        "name": (form.get("name", "") or "").strip(),
        "description": (form.get("description", "") or "").strip(),
        "color": form.get("color", "pine"),
        "target_per_week": form.get("target_per_week", 7),
    }
    try:
        data = HabitIn.validate(payload)
    except Exception as e:
        return layout(
            "New habit",
            f'<div class="panel narrow">{_habit_form(req, "/habits", dict(form.items()), str(e))}</div>',
            "habits",
            await _user_email(req, uid),
            csrf_token(req),
            req=req,
        )
    hid = await req.app.state_db.execute(
        "INSERT INTO habits (user_id, name, description, color, target_per_week, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        uid,
        data.name.strip(),
        data.description.strip(),
        data.color,
        int(data.target_per_week),
        date.today().isoformat(),
    )
    flash(req, f"Habit “{data.name.strip()}” created. Day one starts today.")
    return RedirectResponse(f"/habits/{hid}", status_code=303)


@app.get("/habits/{hid:int}")
async def habit_detail(req, hid: int, uid=Depends(current_user)):
    db = req.app.state_db
    today = date.today()
    h = await _get_habit(db, hid, uid)
    rows = await db.fetch_all("SELECT day FROM checkins WHERE habit_id = ? ORDER BY day DESC", hid)
    done = {r["day"] for r in rows}
    st = habit_stats(done, today, h["target_per_week"])
    color = PALETTE.get(h["color"], PALETTE["pine"])
    csrf = csrf_token(req)
    recent = (
        "".join(f'<li><span>{esc(r["day"])}</span><span class="muted">kept</span></li>' for r in rows[:14])
        or '<li class="muted">No check-ins yet — today is a good day to start.</li>'
    )
    state = "done" if st["done_today"] else "todo"
    body = (
        f'<p class="dateline"><i class="dot" style="background:{color}"></i>'
        f"{esc(h['description']) if h['description'] else 'No description.'}</p>"
        f'<div class="cards"><div class="card"><span>Current streak</span><b>{st["current"]}</b></div>'
        f'<div class="card"><span>Longest</span><b>{st["longest"]}</b></div>'
        f'<div class="card"><span>This week</span><b>{st["week_done"]}/{st["week_target"]}</b></div>'
        f'<div class="card"><span>Last 30 days</span><b>{st["last30_done"]}</b></div></div>'
        f'<div class="panel"><h2>Today</h2>'
        f'<form method="post" action="/check" class="toggle-form inline-form" data-habit="{h["id"]}" data-day="{today.isoformat()}">'
        f'<input type="hidden" name="_csrf_token" value="{csrf}">'
        f'<input type="hidden" name="habit_id" value="{h["id"]}">'
        f'<input type="hidden" name="day" value="{today.isoformat()}">'
        f'<input type="hidden" name="next" value="/habits/{h["id"]}">'
        f'<button class="btn {state}">{"✓ Done today" if st["done_today"] else "Mark today done"}</button>'
        f"</form>"
        f'<form method="post" action="/check" class="backfill">'
        f'<input type="hidden" name="_csrf_token" value="{csrf}">'
        f'<input type="hidden" name="habit_id" value="{h["id"]}">'
        f'<input type="hidden" name="next" value="/habits/{h["id"]}">'
        f'<label>Backfill a day <input type="date" name="day" max="{today.isoformat()}" required></label>'
        f"<button class='ghost'>Toggle</button></form></div>"
        f'<div class="panel"><h2>This week</h2>{week_dots(done, today, color)}'
        f'<h2 class="mt">Last {HEATMAP_WEEKS} weeks</h2>{heatmap(done, today, color)}</div>'
        f'<div class="panel"><h2>Recent check-ins</h2><ul class="recent">{recent}</ul></div>'
        f'<div class="panel danger-zone"><h2>Manage</h2>'
        f'<p><a class="btn small" href="/habits/{h["id"]}/edit">Edit habit</a> '
        f'<form method="post" action="/habits/{h["id"]}/archive" class="inline">'
        f'<input type="hidden" name="_csrf_token" value="{csrf}">'
        f'<button class="ghost">{"Restore" if h["archived"] else "Archive"}</button></form> '
        f'<form method="post" action="/habits/{h["id"]}/delete" class="inline" '
        f"onsubmit=\"return confirm('Delete this habit and all its history?')\">"
        f'<input type="hidden" name="_csrf_token" value="{csrf}">'
        f'<button class="danger">Delete</button></form></p></div>'
    )
    return layout(h["name"], body, "habits", await _user_email(req, uid), csrf, req=req)


@app.get("/habits/{hid:int}/edit")
async def habit_edit_page(req, hid: int, uid=Depends(current_user)):
    h = await _get_habit(req.app.state_db, hid, uid)
    d = dict(h)
    return layout(
        "Edit habit",
        f'<div class="panel narrow">{_habit_form(req, f"/habits/{hid}/edit", d, "", "Save changes")}</div>',
        "habits",
        await _user_email(req, uid),
        csrf_token(req),
        req=req,
    )


@app.post("/habits/{hid:int}/edit")
async def habit_edit(req, hid: int, uid=Depends(current_user)):
    await _get_habit(req.app.state_db, hid, uid)  # 404 unless you own it
    form = await req.form()
    payload = {
        "name": (form.get("name", "") or "").strip(),
        "description": (form.get("description", "") or "").strip(),
        "color": form.get("color", "pine"),
        "target_per_week": form.get("target_per_week", 7),
    }
    try:
        data = HabitIn.validate(payload)
    except Exception as e:
        return layout(
            "Edit habit",
            f'<div class="panel narrow">{_habit_form(req, f"/habits/{hid}/edit", dict(form.items()), str(e), "Save changes")}</div>',
            "habits",
            await _user_email(req, uid),
            csrf_token(req),
            req=req,
        )
    await req.app.state_db.execute(
        "UPDATE habits SET name=?, description=?, color=?, target_per_week=? WHERE id=? AND user_id=?",
        data.name.strip(),
        data.description.strip(),
        data.color,
        int(data.target_per_week),
        hid,
        uid,
    )
    flash(req, "Habit updated.")
    return RedirectResponse(f"/habits/{hid}", status_code=303)


@app.post("/habits/{hid:int}/archive")
async def habit_archive(req, hid: int, uid=Depends(current_user)):
    h = await _get_habit(req.app.state_db, hid, uid)
    await req.app.state_db.execute(
        "UPDATE habits SET archived=? WHERE id=? AND user_id=?",
        0 if h["archived"] else 1,
        hid,
        uid,
    )
    flash(req, "Habit restored." if h["archived"] else "Habit archived. History is kept.")
    return RedirectResponse("/habits", status_code=303)


@app.post("/habits/{hid:int}/delete")
async def habit_delete(req, hid: int, uid=Depends(current_user)):
    await _get_habit(req.app.state_db, hid, uid)
    db = req.app.state_db
    await db.execute(
        "DELETE FROM checkins WHERE habit_id IN (SELECT id FROM habits WHERE id=? AND user_id=?)",
        hid,
        uid,
    )
    await db.execute("DELETE FROM habits WHERE id=? AND user_id=?", hid, uid)
    flash(req, "Habit and its history deleted.")
    return RedirectResponse("/habits", status_code=303)


@app.post("/check")
async def check_toggle(req, uid=Depends(current_user)):
    """Toggle one check-in (HTML forms). JSON clients: POST /api/habits/{id}/toggle."""
    form = await req.form()
    try:
        hid = int(form.get("habit_id", 0))
    except (TypeError, ValueError):
        raise BadRequest("habit_id must be an integer")
    await _get_habit(req.app.state_db, hid, uid)
    day = normalize_day(form.get("day", ""), date.today())
    db = req.app.state_db
    existing = await db.fetch_one("SELECT id FROM checkins WHERE habit_id=? AND day=?", hid, day)
    if existing:
        await db.execute("DELETE FROM checkins WHERE habit_id=? AND day=?", hid, day)
    else:
        await db.execute("INSERT INTO checkins (habit_id, day) VALUES (?, ?)", hid, day)
    return RedirectResponse(safe_next(form.get("next", "/")), status_code=303)


@app.get("/export.csv")
async def export_csv(req, uid=Depends(current_user)):
    rows = await req.app.state_db.fetch_all(
        "SELECT c.day AS day, h.name AS name, h.color AS color FROM checkins c"
        " JOIN habits h ON h.id = c.habit_id WHERE h.user_id = ? ORDER BY c.day, h.name",
        uid,
    )

    def gen():
        yield "date,habit,color\n"
        for r in rows:
            name = '"' + r["name"].replace('"', '""') + '"'
            yield f"{r['day']},{name},{r['color']}\n"

    resp = StreamingResponse(gen(), media_type="text/csv")
    resp.headers["content-disposition"] = "attachment; filename=cadence-export.csv"
    return resp


# ----------------------------------------------------------------------------
# Auth pages
# ----------------------------------------------------------------------------


def _auth_page(kind: str, req, err: str = "", email: str = "") -> HTMLResponse:
    is_login = kind == "login"
    title = "Welcome back" if is_login else "Start your streak"
    action = "/login" if is_login else "/register"
    btn = "Log in" if is_login else "Create account"
    swap = (
        'New to Cadence? <a href="/register">Create an account</a>'
        if is_login
        else 'Already keeping habits? <a href="/login">Log in</a>'
    )
    errh = f'<p class="err">{esc(err)}</p>' if err else ""
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{"Log in" if is_login else "Register"} · Cadence</title>
<link rel="stylesheet" href="/static/style.css">
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml"></head>
<body><div class="auth"><p class="brand big"><span class="mark" aria-hidden="true"></span>Cadence</p>
<p class="tagline">Small habits, kept daily.</p>
<div class="panel"><h2>{title}</h2>{errh}
<form method="post" action="{action}" class="form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<label>Email<input name="email" type="email" value="{esc(email)}" required autocomplete="email"></label>
<label>Password <small>(8+ characters)</small>
<input name="password" type="password" required autocomplete="{"current-password" if is_login else "new-password"}"></label>
<button>{btn}</button></form><p class="muted">{swap}</p></div></div></body></html>"""
    )


@app.get("/register")
async def register_page(req):
    if req.session.get("uid"):
        return RedirectResponse("/", status_code=303)
    return _auth_page("register", req)


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
            data = Signup.validate({"email": form.get("email", ""), "password": form.get("password", "")})
        except Exception as e:
            return _auth_page("register", req, f"validation failed: {e}", form.get("email", ""))
    uid = secrets.token_hex(8)
    try:
        await req.app.state_db.execute(
            "INSERT INTO users (id, email, pw) VALUES (?, ?, ?)",
            uid,
            data.email,
            hash_password(data.password),
        )
    except Exception:
        existing = await req.app.state_db.fetch_one("SELECT id FROM users WHERE email = ?", data.email)
        if existing is None:
            raise
        if is_json:
            raise BadRequest("email already registered")
        return _auth_page("register", req, "email already registered", data.email)
    req.session["uid"] = uid
    bg.add(print, f"welcome {data.email}")
    flash(req, f"Account created — welcome, {data.email}. Create your first habit.")
    if "text/html" in req.headers.get("accept", ""):
        return RedirectResponse("/habits/new", status_code=303)
    return {"uid": uid, "email": data.email}, 201


@app.get("/login")
async def login_page(req):
    if req.session.get("uid"):
        return RedirectResponse("/", status_code=303)
    return _auth_page("login", req)


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
            raise Unauthorized("bad credentials")
        return _auth_page("login", req, "Invalid email or password.", email)
    req.session["uid"] = row["id"]
    flash(req, f"Welcome back, {row['email']}.")
    if "text/html" in req.headers.get("accept", ""):
        return RedirectResponse("/", status_code=303)
    return {"uid": row["id"], "email": row["email"]}


@app.post("/logout")
async def logout(req):
    req.session.clear()
    flash(req, "Logged out.")
    if "text/html" in req.headers.get("accept", "") or "multipart/form-data" in req.headers.get(
        "content-type", ""
    ):
        return RedirectResponse("/login", status_code=303)
    return {"ok": True}


# ----------------------------------------------------------------------------
# JSON API
# ----------------------------------------------------------------------------


@app.get("/api/csrf")
async def api_csrf(req):
    return {"csrf": csrf_token(req)}


def _habit_json(h, done: set, today: date) -> dict:
    return {
        "id": h["id"],
        "name": h["name"],
        "description": h["description"],
        "color": h["color"],
        "target_per_week": h["target_per_week"],
        "archived": bool(h["archived"]),
        "created_at": h["created_at"],
        "stats": habit_stats(done, today, h["target_per_week"]),
    }


@app.get("/api/summary")
async def api_summary(req, uid=Depends(current_user)):
    """Today's progress plus per-habit streak stats as JSON."""
    today = date.today()
    habits = await _habits_for(req.app.state_db, uid)
    by_habit = await _days_by_habit(req.app.state_db, uid)
    items = [_habit_json(h, by_habit.get(h["id"], set()), today) for h in habits]
    done_today = sum(1 for i in items if i["stats"]["done_today"])
    return {
        "today": today.isoformat(),
        "total": len(items),
        "done_today": done_today,
        "habits": items,
    }


@app.get("/api/habits")
async def api_list(req, uid=Depends(current_user)):
    """List active habits with streak stats."""
    today = date.today()
    habits = await _habits_for(req.app.state_db, uid)
    by_habit = await _days_by_habit(req.app.state_db, uid)
    return [_habit_json(h, by_habit.get(h["id"], set()), today) for h in habits]


@app.post("/api/habits")
async def api_create(req, habit: HabitIn, uid=Depends(current_user)):
    """Create a habit (JSON)."""
    hid = await req.app.state_db.execute(
        "INSERT INTO habits (user_id, name, description, color, target_per_week, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        uid,
        habit.name.strip(),
        (habit.description or "").strip(),
        habit.color,
        int(habit.target_per_week),
        date.today().isoformat(),
    )
    return {"id": hid, "name": habit.name.strip()}, 201


@app.get("/api/habits/{hid:int}")
async def api_get(req, hid: int, uid=Depends(current_user)):
    """One habit with stats and its last 30 check-in days."""
    h = await _get_habit(req.app.state_db, hid, uid)
    today = date.today()
    rows = await req.app.state_db.fetch_all(
        "SELECT day FROM checkins WHERE habit_id = ? ORDER BY day DESC LIMIT 90", hid
    )
    all_rows = await req.app.state_db.fetch_all("SELECT day FROM checkins WHERE habit_id = ?", hid)
    done = {r["day"] for r in all_rows}
    out = _habit_json(h, done, today)
    out["recent"] = [r["day"] for r in rows]
    return out


@app.put("/api/habits/{hid:int}")
async def api_update(req, hid: int, uid=Depends(current_user)):
    """Replace a habit (full HabitIn body)."""
    body = await req.json()
    try:
        data = HabitIn.validate(body)
    except Exception as e:
        raise BadRequest(f"validation failed: {e}")
    await _get_habit(req.app.state_db, hid, uid)
    await req.app.state_db.execute(
        "UPDATE habits SET name=?, description=?, color=?, target_per_week=? WHERE id=? AND user_id=?",
        data.name.strip(),
        (data.description or "").strip(),
        data.color,
        int(data.target_per_week),
        hid,
        uid,
    )
    return {"id": hid, "ok": True}


@app.delete("/api/habits/{hid:int}")
async def api_delete(req, hid: int, uid=Depends(current_user)):
    """Delete a habit and its history."""
    await _get_habit(req.app.state_db, hid, uid)
    db = req.app.state_db
    await db.execute("DELETE FROM checkins WHERE habit_id=?", hid)
    await db.execute("DELETE FROM habits WHERE id=? AND user_id=?", hid, uid)
    return {"ok": True}


@app.post("/api/habits/{hid:int}/toggle")
async def api_toggle(req, hid: int, uid=Depends(current_user)):
    """Toggle one check-in day (defaults to today)."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    try:
        data = ToggleIn.validate({"day": body.get("day", "") or ""})
    except Exception as e:
        raise BadRequest(f"validation failed: {e}")
    await _get_habit(req.app.state_db, hid, uid)
    day = normalize_day(data.day, date.today())
    db = req.app.state_db
    existing = await db.fetch_one("SELECT id FROM checkins WHERE habit_id=? AND day=?", hid, day)
    if existing:
        await db.execute("DELETE FROM checkins WHERE habit_id=? AND day=?", hid, day)
        done_now = False
    else:
        await db.execute("INSERT INTO checkins (habit_id, day) VALUES (?, ?)", hid, day)
        done_now = True
    rows = await db.fetch_all("SELECT day FROM checkins WHERE habit_id = ?", hid)
    h = await _get_habit(db, hid, uid)
    st = habit_stats({r["day"] for r in rows}, date.today(), h["target_per_week"])
    return {"id": hid, "day": day, "done": done_now, "current_streak": st["current"]}


@app.get("/api/history")
async def api_history(req, uid=Depends(current_user)):
    """Check-in days for the last N days (default 84)."""
    try:
        days = max(1, min(365, int(req.query.get("days", "84"))))
    except ValueError:
        raise BadRequest("days must be an integer")
    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = await req.app.state_db.fetch_all(
        "SELECT c.habit_id AS habit_id, c.day AS day FROM checkins c"
        " JOIN habits h ON h.id = c.habit_id WHERE h.user_id = ? AND c.day >= ?"
        " ORDER BY c.day",
        uid,
        cutoff,
    )
    return {"days": days, "checkins": rows}
