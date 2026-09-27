"""Ledger — personal finance on IKAREM. Dashboard + CRUD + uploads + JSON API."""

import re
import secrets
from datetime import date
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
    hash_password,
    verify_token,
)
from ikarem.db import DatabasePlugin
from ikarem.http import HTMLResponse, RedirectResponse, StreamingResponse, UploadFile
from ikarem.static import FileResponse

BASE = Path(__file__).parent
UPLOADS = BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)

app = Ikarem(session_secret="ledger-dev-secret-change-in-prod", db_url="sqlite:///ledger.db")
app.use(RequestIDMiddleware())
app.use(SecurityHeadersMiddleware())
app.use(CORSMiddleware())
app.use(SessionMiddleware())
app.use(CSRFMiddleware())
app.use(RateLimitMiddleware(per_minute=240))
app.register(DatabasePlugin(app.config.get("db_url", "sqlite:///ledger.db")))
app.mount_static("/static", str(BASE / "static"))

CATEGORIES = {
    "Salary": "#34d399",
    "Freelance": "#2dd4bf",
    "Food": "#fb923c",
    "Rent": "#a78bfa",
    "Transport": "#60a5fa",
    "Shopping": "#f472b6",
    "Health": "#4ade80",
    "Fun": "#facc15",
    "Utilities": "#94a3b8",
    "Other": "#64748b",
}


@app.on_startup
async def init_db():
    db = app.state_db
    await db.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE, pw TEXT)")
    await db.execute(
        "CREATE TABLE IF NOT EXISTS txns (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT,"
        " day TEXT, description TEXT, amount_cents INTEGER, kind TEXT, category TEXT, receipt TEXT)"
    )
    if str(app.config.get("demo", "true")).lower() in ("1", "true", "yes", "on"):
        if await db.fetch_one("SELECT id FROM users LIMIT 1") is None:
            await _seed_demo(db)


async def _seed_demo(db):
    import random

    rng = random.Random(7)
    uid = "demo-user"
    await db.execute(
        "INSERT INTO users (id, email, pw) VALUES (?, ?, ?)",
        uid,
        "demo@example.com",
        hash_password("demo1234"),
    )
    items = [
        ("Salary", "income", 420000, 520000),
        ("Freelance", "income", 40000, 180000),
        ("Food", "expense", 1200, 6500),
        ("Rent", "expense", 145000, 145000),
        ("Transport", "expense", 2500, 9000),
        ("Shopping", "expense", 3000, 22000),
        ("Health", "expense", 2000, 12000),
        ("Fun", "expense", 1500, 11000),
        ("Utilities", "expense", 8000, 14000),
    ]
    names = {
        "Food": ["Groceries", "Coffee", "Restaurant", "Takeout"],
        "Transport": ["Metro card", "Taxi", "Gas"],
        "Shopping": ["Sneakers", "Books", "Headphones"],
        "Fun": ["Cinema", "Concert", "Games"],
        "Health": ["Pharmacy", "Gym"],
        "Utilities": ["Electricity", "Internet", "Water"],
        "Rent": ["Monthly rent"],
        "Salary": ["Monthly salary"],
        "Freelance": ["Client project", "Consulting"],
    }
    today = date.today()
    for m in range(6):
        d = date(today.year, today.month, 1)
        for _ in range(m):
            d = date(d.year - 1, 12, 1) if d.month == 1 else date(d.year, d.month - 1, 1)
        for _ in range(rng.randint(7, 11)):
            cat, kind, lo, hi = items[rng.randrange(len(items))]
            day = f"{d.year}-{d.month:02d}-{rng.randint(1, 28):02d}"
            await db.execute(
                "INSERT INTO txns (user_id, day, description, amount_cents, kind, category)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                uid,
                day,
                rng.choice(names[cat]),
                rng.randint(lo, hi),
                kind,
                cat,
            )


class Signup(Schema):
    email: str = Field(..., email=True, max_length=254)
    password: str = Field(..., min_length=8, max_length=128)


class TxnIn(Schema):
    description: str = Field(..., min_length=1, max_length=200)
    amount: float = Field(..., gt=0, le=10_000_000, description="Dollars, e.g. 12.50")
    kind: str = Field("expense", pattern=r"^(income|expense)$")
    category: str = Field("Other", min_length=1, max_length=40)
    day: str = Field("", pattern=r"^(\d{4}-\d{2}-\d{2})?$")


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


class LoginRequired(Unauthorized):
    """Anonymous visitor: browsers redirect to /login, APIs get JSON 401."""


@app.exception_handler(LoginRequired)
async def login_required_handler(req, exc):
    if wants_html(req):
        return RedirectResponse("/login", status_code=303)
    from ikarem.http import JSONResponse

    return JSONResponse({"detail": "login required"}, status_code=401)


def wants_html(req) -> bool:
    return "text/html" in req.headers.get("accept", "")


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return f"{sign}${c // 100:,}.{c % 100:02d}"


def esc(s: object) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def layout(title: str, body: str, active: str = "", email: str | None = None, csrf: str = "") -> HTMLResponse:
    nav = "".join(
        f'<a href="{href}" class="{"on" if active == key else ""}">{label}</a>'
        for key, href, label in [
            ("dash", "/", "Dashboard"),
            ("txns", "/txns", "Transactions"),
            ("new", "/txns/new", "+ New"),
            ("export", "/export.csv", "Export"),
            ("docs", "/docs", "API"),
        ]
    )
    user = (
        f'<span class="who">{esc(email)}</span>'
        f'<form method="post" action="/logout" class="inline"><input type="hidden" name="_csrf_token" value="{csrf}">'
        f'<button class="ghost">Logout</button></form>'
        if email
        else '<a href="/login">Login</a>'
    )
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · Ledger</title><link rel="stylesheet" href="/static/style.css"><link rel="icon" href="/static/favicon.svg"></head>
<body><div class="app"><aside><div class="brand">◈ Ledger</div><nav>{nav}</nav>
<div class="side-foot">{user}</div></aside><main><h1>{esc(title)}</h1>{body}</main></div></body></html>""")


def month_bars(monthly: list[dict]) -> str:
    W, H, base = 560, 200, 170
    mx = max([m["income"] for m in monthly] + [m["expense"] for m in monthly] + [1])
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img">']
    for i, m in enumerate(monthly):
        x = 20 + i * 90
        ih = round(m["income"] / mx * 130)
        eh = round(m["expense"] / mx * 130)
        out.append(
            f'<rect x="{x}" y="{base - ih}" width="34" height="{ih}" rx="4" class="inc"/>'
            f'<rect x="{x + 38}" y="{base - eh}" width="34" height="{eh}" rx="4" class="exp"/>'
            f'<text x="{x + 36}" y="{base + 18}" class="lbl">{m["month"][2:]}</text>'
        )
    out.append(
        '<g class="legend"><circle cx="400" cy="14" r="5" class="inc"/><text x="410" y="18">income</text>'
        '<circle cx="470" cy="14" r="5" class="exp"/><text x="480" y="18">spent</text></g></svg>'
    )
    return "".join(out)


def donut(cats: list[dict], total: int) -> str:
    if not total:
        return '<p class="muted">No expenses yet.</p>'
    R, C = 54, 2 * 3.14159 * 54
    segs, off, rows = [], 0.0, []
    for c in cats:
        frac = c["total"] / total
        segs.append(
            f'<circle r="{R}" cx="70" cy="70" fill="none" stroke="{CATEGORIES.get(c["category"], "#64748b")}"'
            f' stroke-width="18" stroke-dasharray="{frac * C:.1f} {C:.1f}"'
            f' stroke-dashoffset="{-off * C:.1f}" transform="rotate(-90 70 70)"/>'
        )
        off += frac
        rows.append(
            f'<li><i style="background:{CATEGORIES.get(c["category"], "#64748b")}"></i>{esc(c["category"])}'
            f"<b>{money(c['total'])}</b><span>{frac:.0%}</span></li>"
        )
    return (
        f'<div class="donut-wrap"><svg viewBox="0 0 140 140" class="donut">{"".join(segs)}</svg>'
        f'<ul class="donut-legend">{"".join(rows)}</ul></div>'
    )


async def _user_email(req, uid: str) -> str:
    row = await req.app.state_db.fetch_one("SELECT email FROM users WHERE id = ?", uid)
    return row["email"] if row else "?"


def _filters(req, form=None):
    src = form or {}
    return {
        "q": req.query.get("q", src.get("q", "")),
        "kind": req.query.get("kind", src.get("kind", "")),
        "cat": req.query.get("cat", src.get("cat", "")),
        "month": req.query.get("month", src.get("month", "")),
        "page": max(1, int(req.query.get("page", "1") or 1)),
    }


async def _query_txns(db, uid: str, f: dict, per: int = 12):
    where, args = ["user_id = ?"], [uid]
    if f["q"]:
        where.append("description LIKE ?")
        args.append(f"%{f['q']}%")
    if f["kind"] in ("income", "expense"):
        where.append("kind = ?")
        args.append(f["kind"])
    if f["cat"]:
        where.append("category = ?")
        args.append(f["cat"])
    if re.fullmatch(r"\d{4}-\d{2}", f["month"] or ""):
        where.append("substr(day, 1, 7) = ?")
        args.append(f["month"])
    w = " AND ".join(where)
    total = (await db.fetch_one(f"SELECT COUNT(*) AS n FROM txns WHERE {w}", *args))["n"]
    pages = max(1, (total + per - 1) // per)
    page = min(f["page"], pages)
    rows = await db.fetch_all(
        f"SELECT * FROM txns WHERE {w} ORDER BY day DESC, id DESC LIMIT ? OFFSET ?",
        *args,
        per,
        (page - 1) * per,
    )
    return rows, total, pages, page


def _filter_bar(f: dict) -> str:
    cats = "".join(
        f'<option value="{c}"{" selected" if f["cat"] == c else ""}>{c}</option>' for c in CATEGORIES
    )
    return (
        f'<form method="get" action="/txns" class="filters">'
        f'<input name="q" placeholder="Search…" value="{esc(f["q"])}">'
        f'<select name="kind"><option value="">All</option>'
        f'<option value="income"{" selected" if f["kind"] == "income" else ""}>Income</option>'
        f'<option value="expense"{" selected" if f["kind"] == "expense" else ""}>Expenses</option></select>'
        f'<select name="cat"><option value="">Category</option>{cats}</select>'
        f'<input name="month" type="month" value="{esc(f["month"])}">'
        f"<button>Filter</button></form>"
    )


def _txn_rows(rows) -> str:
    out = []
    for r in rows:
        amt = r["amount_cents"] if r["kind"] == "income" else -r["amount_cents"]
        cls = "pos" if amt >= 0 else "neg"
        rec = f' <a class="mini" href="/receipts/{r["id"]}">🧾</a>' if r["receipt"] else ""
        out.append(
            f'<tr><td class="d">{esc(r["day"])}</td><td>{esc(r["description"])}{rec}</td>'
            f'<td><span class="pill">{esc(r["category"])}</span></td>'
            f'<td class="r {cls}">{money(amt)}</td>'
            f'<td class="r"><a class="mini" href="/txns/{r["id"]}/edit">edit</a></td></tr>'
        )
    return "".join(out) or '<tr><td colspan="5" class="muted">Nothing here yet.</td></tr>'


@app.get("/")
async def dashboard(req, uid=Depends(current_user)):
    db = req.app.state_db
    email = await _user_email(req, uid)
    sums = await db.fetch_one(
        "SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount_cents END),0) AS i,"
        " COALESCE(SUM(CASE WHEN kind='expense' THEN amount_cents END),0) AS e FROM txns WHERE user_id = ?",
        uid,
    )
    monthly = await db.fetch_all(
        "SELECT substr(day,1,7) AS month,"
        " COALESCE(SUM(CASE WHEN kind='income' THEN amount_cents END),0) AS income,"
        " COALESCE(SUM(CASE WHEN kind='expense' THEN amount_cents END),0) AS expense"
        " FROM txns WHERE user_id = ? GROUP BY month ORDER BY month DESC LIMIT 6",
        uid,
    )
    monthly.reverse()
    cats = await db.fetch_all(
        "SELECT category, SUM(amount_cents) AS total FROM txns WHERE user_id = ? AND kind='expense'"
        " GROUP BY category ORDER BY total DESC",
        uid,
    )
    recent = await db.fetch_all(
        "SELECT * FROM txns WHERE user_id = ? ORDER BY day DESC, id DESC LIMIT 8", uid
    )
    exp_total = sum(c["total"] for c in cats)
    body = (
        f'<div class="cards"><div class="card"><span>Balance</span><b class="{"pos" if sums["i"] - sums["e"] >= 0 else "neg"}">'
        f"{money(sums['i'] - sums['e'])}</b></div>"
        f'<div class="card"><span>Income</span><b class="pos">{money(sums["i"])}</b></div>'
        f'<div class="card"><span>Spent</span><b class="neg">{money(sums["e"])}</b></div></div>'
        f'<div class="grid"><div class="panel"><h2>Last 6 months</h2>{month_bars(monthly)}</div>'
        f'<div class="panel"><h2>Spending by category</h2>{donut(cats, exp_total)}</div></div>'
        f'<div class="panel"><h2>Recent</h2><table>{_txn_rows(recent)}</table></div>'
    )
    return layout("Dashboard", body, "dash", email, csrf_token(req))


@app.get("/txns")
async def txns_page(req, uid=Depends(current_user)):
    f = _filters(req)
    rows, total, pages, page = await _query_txns(req.app.state_db, uid, f)
    q = f"&q={f['q']}&kind={f['kind']}&cat={f['cat']}&month={f['month']}"
    prev_link = f'<a href="/txns?page={page - 1}{q}">\u2190 prev</a>' if page > 1 else ""
    next_link = f'<a href="/txns?page={page + 1}{q}">next \u2192</a>' if page < pages else ""
    pager = (
        f'<div class="pager">Page {page} of {pages} · {total} transactions · {prev_link} {next_link}</div>'
    )
    return layout(
        "Transactions",
        _filter_bar(f) + f'<div class="panel"><table>{_txn_rows(rows)}</table></div>' + pager,
        "txns",
        await _user_email(req, uid),
        csrf_token(req),
    )


def _txn_form(req, action: str, d: dict | None = None, err: str = "") -> str:
    d = d or {}
    cats = "".join(f"<option{' selected' if d.get('category') == c else ''}>{c}</option>" for c in CATEGORIES)
    errh = f'<p class="err">{esc(err)}</p>' if err else ""
    return (
        f'{errh}<form method="post" action="{action}" enctype="multipart/form-data" class="form">'
        f'<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">'
        f'<label>Description<input name="description" value="{esc(d.get("description", ""))}" required></label>'
        f'<div class="row"><label>Amount $<input name="amount" value="{esc(d.get("amount", ""))}" required></label>'
        f'<label>Type<select name="kind"><option value="expense">Expense</option>'
        f'<option value="income"{" selected" if d.get("kind") == "income" else ""}>Income</option></select></label></div>'
        f'<div class="row"><label>Category<select name="category">{cats}</select></label>'
        f'<label>Date<input name="day" type="date" value="{esc(d.get("day", ""))}"></label></div>'
        f'<label>Receipt (optional)<input type="file" name="receipt"></label>'
        f"<button>Add transaction</button></form>"
    )


def _to_cents(amount: float) -> int:
    return int(round(float(amount) * 100))


def _safe_filename(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[-60:]
    return safe or "receipt"


async def _save_receipt(form) -> str | None:
    f = form.get("receipt")
    if not isinstance(f, UploadFile) or not f.filename:
        return None
    data = await f.read()
    if not data:
        return None
    name = f"{secrets.token_hex(6)}_{_safe_filename(f.filename)}"
    (UPLOADS / name).write_bytes(data)
    return name


@app.get("/txns/new")
async def txn_new(req, uid=Depends(current_user)):
    return layout(
        "New transaction",
        f'<div class="panel">{_txn_form(req, "/txns")}</div>',
        "new",
        await _user_email(req, uid),
        csrf_token(req),
    )


@app.post("/txns")
async def txn_create(req, uid=Depends(current_user)):
    form = await req.form(max_file_size=5 * 1024 * 1024)
    try:
        data = TxnIn.validate(
            {
                "description": form.get("description", ""),
                "amount": form.get("amount", ""),
                "kind": form.get("kind", "expense"),
                "category": form.get("category", "Other"),
                "day": form.get("day", "") or "",
            }
        )
    except Exception as e:
        return layout(
            "New transaction",
            f'<div class="panel">{_txn_form(req, "/txns", dict(form.items()), str(e))}</div>',
            "new",
            await _user_email(req, uid),
            csrf_token(req),
        )
    day = data.day or date.today().isoformat()
    receipt = await _save_receipt(form)
    await req.app.state_db.execute(
        "INSERT INTO txns (user_id, day, description, amount_cents, kind, category, receipt)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        uid,
        day,
        data.description,
        _to_cents(data.amount),
        data.kind,
        data.category,
        receipt,
    )
    return RedirectResponse("/txns", status_code=303)


@app.get("/txns/{tid:int}/edit")
async def txn_edit_page(req, tid: int, uid=Depends(current_user)):
    row = await req.app.state_db.fetch_one("SELECT * FROM txns WHERE id = ? AND user_id = ?", tid, uid)
    if not row:
        raise NotFound("transaction not found")
    d = dict(row)
    d["amount"] = f"{d['amount_cents'] / 100:.2f}"
    form = _txn_form(req, f"/txns/{tid}/edit", d, "").replace("Add transaction", "Save changes")
    return layout(
        "Edit transaction",
        f'<div class="panel">{form}</div>',
        "txns",
        await _user_email(req, uid),
        csrf_token(req),
    )


@app.post("/txns/{tid:int}/edit")
async def txn_edit(req, tid: int, uid=Depends(current_user)):
    row = await req.app.state_db.fetch_one("SELECT * FROM txns WHERE id = ? AND user_id = ?", tid, uid)
    if not row:
        raise NotFound("transaction not found")
    form = await req.form(max_file_size=5 * 1024 * 1024)
    try:
        data = TxnIn.validate(
            {
                "description": form.get("description", ""),
                "amount": form.get("amount", ""),
                "kind": form.get("kind", "expense"),
                "category": form.get("category", "Other"),
                "day": form.get("day", "") or row["day"],
            }
        )
    except Exception as e:
        return layout(
            "Edit transaction",
            f'<div class="panel">{_txn_form(req, f"/txns/{tid}/edit", dict(form.items()), str(e))}</div>',
            "txns",
            await _user_email(req, uid),
            csrf_token(req),
        )
    receipt = await _save_receipt(form) or row["receipt"]
    await req.app.state_db.execute(
        "UPDATE txns SET day=?, description=?, amount_cents=?, kind=?, category=?, receipt=? WHERE id=? AND user_id=?",
        data.day or row["day"],
        data.description,
        _to_cents(data.amount),
        data.kind,
        data.category,
        receipt,
        tid,
        uid,
    )
    return RedirectResponse("/txns", status_code=303)


@app.post("/txns/{tid:int}/delete")
async def txn_delete(req, tid: int, uid=Depends(current_user)):
    await req.app.state_db.execute("DELETE FROM txns WHERE id = ? AND user_id = ?", tid, uid)
    return RedirectResponse("/txns", status_code=303)


@app.get("/receipts/{tid:int}")
async def receipt(req, tid: int, uid=Depends(current_user)):
    row = await req.app.state_db.fetch_one("SELECT receipt FROM txns WHERE id = ? AND user_id = ?", tid, uid)
    if not row or not row["receipt"]:
        raise NotFound("receipt not found")
    path = UPLOADS / row["receipt"]
    if not path.is_file():
        raise NotFound("receipt not found")
    return FileResponse(str(path))


@app.get("/export.csv")
async def export_csv(req, uid=Depends(current_user)):
    rows = await req.app.state_db.fetch_all(
        "SELECT day, description, amount_cents, kind, category FROM txns WHERE user_id = ? ORDER BY day, id",
        uid,
    )

    def gen():
        yield "date,description,amount_cents,kind,category\n"
        for r in rows:
            desc = '"' + r["description"].replace('"', '""') + '"'
            yield f"{r['day']},{desc},{r['amount_cents']},{r['kind']},{r['category']}\n"

    resp = StreamingResponse(gen(), media_type="text/csv")
    resp.headers["content-disposition"] = "attachment; filename=ledger.csv"
    return resp


@app.get("/register")
async def register_page(req):
    return _auth_page("register", req)


def _auth_page(kind: str, req, err: str = "", email: str = "") -> HTMLResponse:
    is_login = kind == "login"
    title = "Welcome back" if is_login else "Create account"
    action = "/login" if is_login else "/register"
    btn = "Login" if is_login else "Create account"
    pw_label = "Password" if is_login else "Password <small>(8+ chars)</small>"
    swap = (
        'New here? <a href="/register">Register</a> · '
        '<span class="demo">demo: demo@example.com / demo1234</span>'
        if is_login
        else 'Have an account? <a href="/login">Login</a>'
    )
    errh = f'<p class="err">{esc(err)}</p>' if err else ""
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{"Login" if is_login else "Register"} · Ledger</title><link rel="stylesheet" href="/static/style.css"><link rel="icon" href="/static/favicon.svg"></head><body><div class="auth">
<h1>◈ Ledger</h1><div class="panel"><h2>{title}</h2>{errh}
<form method="post" action="{action}" class="form"><input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<label>Email<input name="email" type="email" value="{esc(email)}" required></label>
<label>{pw_label}<input name="password" type="password" required></label>
<button>{btn}</button></form><p class="muted">{swap}</p></div></div></body></html>""")


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
        if is_json:
            raise BadRequest("email already registered")
        return _auth_page("register", req, "email already registered", data.email)
    req.session["uid"] = uid
    bg.add(print, f"welcome {data.email}")
    if "text/html" in req.headers.get("accept", ""):
        return RedirectResponse("/", status_code=303)
    return {"uid": uid, "email": data.email}, 201


@app.get("/login")
async def login_page(req):
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
    if "text/html" in req.headers.get("accept", ""):
        return RedirectResponse("/", status_code=303)
    return {"uid": row["id"], "email": row["email"]}


@app.post("/logout")
async def logout(req):
    req.session.clear()
    if "text/html" in req.headers.get("accept", "") or "multipart/form-data" in req.headers.get(
        "content-type", ""
    ):
        return RedirectResponse("/login", status_code=303)
    return {"ok": True}


@app.get("/api/csrf")
async def api_csrf(req):
    return {"csrf": csrf_token(req)}


@app.get("/api/summary")
async def api_summary(req, uid=Depends(current_user)):
    """Dashboard numbers as JSON."""
    db = req.app.state_db
    sums = await db.fetch_one(
        "SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount_cents END),0) AS income,"
        " COALESCE(SUM(CASE WHEN kind='expense' THEN amount_cents END),0) AS expense"
        " FROM txns WHERE user_id = ?",
        uid,
    )
    cats = await db.fetch_all(
        "SELECT category, SUM(amount_cents) AS total FROM txns WHERE user_id = ? AND kind='expense'"
        " GROUP BY category ORDER BY total DESC",
        uid,
    )
    return {
        "income_cents": sums["income"],
        "expense_cents": sums["expense"],
        "balance_cents": sums["income"] - sums["expense"],
        "by_category": cats,
    }


@app.get("/api/txns")
async def api_list(req, uid=Depends(current_user)):
    """List transactions with q/kind/cat/month/page filters."""
    f = _filters(req)
    rows, total, pages, page = await _query_txns(req.app.state_db, uid, f, per=20)
    return {"total": total, "pages": pages, "page": page, "txns": rows}


@app.post("/api/txns")
async def api_create(req, txn: TxnIn, uid=Depends(current_user)):
    """Create a transaction (JSON)."""
    day = txn.day or date.today().isoformat()
    cur = await req.app.state_db.execute(
        "INSERT INTO txns (user_id, day, description, amount_cents, kind, category) VALUES (?, ?, ?, ?, ?, ?)",
        uid,
        day,
        txn.description,
        _to_cents(txn.amount),
        txn.kind,
        txn.category,
    )
    return {
        "id": cur,
        "day": day,
        "description": txn.description,
        "amount_cents": _to_cents(txn.amount),
        "kind": txn.kind,
        "category": txn.category,
    }, 201


@app.put("/api/txns/{tid:int}")
async def api_update(req, tid: int, uid=Depends(current_user)):
    body = await req.json()
    row = await req.app.state_db.fetch_one("SELECT * FROM txns WHERE id = ? AND user_id = ?", tid, uid)
    if not row:
        raise NotFound("transaction not found")
    await req.app.state_db.execute(
        "UPDATE txns SET description=?, kind=?, category=? WHERE id=? AND user_id=?",
        body.get("description", row["description"]),
        body.get("kind", row["kind"]),
        body.get("category", row["category"]),
        tid,
        uid,
    )
    return {"id": tid, "ok": True}


@app.delete("/api/txns/{tid:int}")
async def api_delete(req, tid: int, uid=Depends(current_user)):
    await req.app.state_db.execute("DELETE FROM txns WHERE id = ? AND user_id = ?", tid, uid)
    return {"ok": True}
