"""BAZAAR — town square market on stock IKAREM.

Shops + catalog + cart + atomic idempotent checkout + order pipeline +
reviews + coupons + wallet + seller desk + admin moderation + live sold
ticker + JSON API with NISH from day one. One SQLite file.

Run:  uvicorn market.app:app   (demo: buyer@bazaar.local / buyer1234,
      seller@ / seller1234, admin@ / admin1234 — see README)
Test: python -m pytest market/tests -q
"""

from __future__ import annotations

import hashlib
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
    Forbidden,
    IdempotencyMiddleware,
    Ikarem,
    MemoryCache,
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
TICKER = Room()

CATS = ["Produce", "Bakery", "Pantry", "Craft", "Cloth", "Tools", "Books", "Other"]
CAT_INK = {
    "Produce": "#2E5339",
    "Bakery": "#9A6A00",
    "Pantry": "#BC3F0C",
    "Craft": "#6B3FA0",
    "Cloth": "#31556E",
    "Tools": "#5B5346",
    "Books": "#7F1D1D",
    "Other": "#8A8171",
}
FLOW = ["placed", "paid", "packed", "shipped", "delivered"]
NEXT = {"placed": "paid", "paid": "packed", "packed": "shipped", "shipped": "delivered"}
OPEN = ("placed", "paid", "packed", "shipped")
PER_PAGE = 12


# ---------------------------------------------------------------- schemas


class Signup(Schema):
    email: str = Field(..., email=True, max_length=254)
    password: str = Field(..., min_length=8, max_length=128)
    name: str = Field("", max_length=60)


class ShopIn(Schema):
    name: str = Field(..., min_length=2, max_length=80)
    blurb: str = Field("", max_length=300)


class ProductIn(Schema):
    name: str = Field(..., min_length=2, max_length=120)
    detail: str = Field("", max_length=2000)
    price: float = Field(..., gt=0, le=1_000_000)
    stock: int = Field(0, ge=0, le=1_000_000)
    category: str = Field("Other", min_length=1, max_length=40)


class ReviewIn(Schema):
    stars: int = Field(..., ge=1, le=5)
    body: str = Field("", max_length=2000)


class CouponIn(Schema):
    code: str = Field(..., min_length=3, max_length=24, pattern=r"^[A-Z0-9-]+$")
    pct: int = Field(..., ge=1, le=90)
    max_uses: int = Field(..., ge=1, le=100000)


class TopupIn(Schema):
    amount: float = Field(..., gt=0, le=100000)


class RoleIn(Schema):
    role: str = Field(..., pattern=r"^(buyer|seller|admin)$")


# ---------------------------------------------------------------- durable work


@task("bazaar-receipt")
async def bazaar_receipt(payload: dict) -> None:
    """Durable paper receipt per order. Drain: ikarem worker market.app:app."""
    code = re.sub(r"[^A-Za-z0-9-]", "_", str(payload.get("code", "order")))[:40]
    (MAILBOX / f"{code}.txt").write_text(
        f"BAZAAR receipt {code} — {datetime.now().isoformat(timespec='seconds')}\n"
        f"Buyer {payload.get('buyer', '?')} paid ${float(payload.get('total', 0)):.2f} "
        f"for {payload.get('lines', '?')} lines.\n",
        encoding="utf-8",
    )


def create_app(db_url: str = "sqlite:///bazaar.db", demo: bool = True) -> Ikarem:
    app = Ikarem(
        session_secret="bazaar-square-secret-change-in-prod",
        auth_secret="bazaar-jwt-secret-change-in-prod",
        db_url=db_url,
        demo="true" if demo else "false",
        version="1.0.0",
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
            row = await db.fetch_one(
                "SELECT COALESCE(SUM(total),0) AS g FROM orders WHERE status != 'cancelled'"
            )
            await db.execute(
                "INSERT INTO activity (user_id, kind, text, created) VALUES (?, ?, ?, ?)",
                "system",
                "digest",
                f"Square digest: lifetime turnover {money(row['g'])}.",
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
        " pw TEXT, name TEXT, role TEXT, wallet_cents INTEGER, created TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS shops (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " owner_id TEXT, name TEXT, blurb TEXT, created TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " shop_id INTEGER, name TEXT, detail TEXT, price_cents INTEGER, stock INTEGER,"
        " category TEXT, image TEXT, status TEXT, created TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS coupons (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " code TEXT UNIQUE, pct INTEGER, max_uses INTEGER, used INTEGER, active INTEGER)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS cart (user_id TEXT, product_id INTEGER, qty INTEGER,"
        " added TEXT, PRIMARY KEY (user_id, product_id))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " code TEXT UNIQUE, buyer_id TEXT, subtotal INTEGER, discount INTEGER, total INTEGER,"
        " status TEXT, coupon TEXT, address TEXT, created TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS order_items (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " order_id INTEGER, product_id INTEGER, shop_id INTEGER, name TEXT, price_cents INTEGER, qty INTEGER)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS order_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " order_id INTEGER, status TEXT, at TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " product_id INTEGER, user_id TEXT, name TEXT, stars INTEGER, body TEXT, created TEXT,"
        " UNIQUE(product_id, user_id))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS wishlist (user_id TEXT, product_id INTEGER,"
        " PRIMARY KEY (user_id, product_id))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id TEXT, kind TEXT, text TEXT, created TEXT)"
    )
    demo_on = str(app.config.get("demo", "true")).lower() in ("1", "true", "yes", "on")
    if demo_on and await db.fetch_one("SELECT id FROM users LIMIT 1") is None:
        await _seed_demo(db)


async def _mkuser(db, email: str, pw: str, name: str, role: str, wallet: int) -> str:
    uid = secrets.token_hex(8)
    await db.execute(
        "INSERT INTO users (id, email, pw, name, role, wallet_cents, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
        uid,
        email,
        hash_password(pw),
        name,
        role,
        wallet,
        _now(),
    )
    return uid


async def _seed_demo(db) -> None:
    import random

    rng = random.Random(11)
    admin = await _mkuser(db, "admin@bazaar.local", "admin1234", "Warden", "admin", 0)
    s1 = await _mkuser(db, "seller@bazaar.local", "seller1234", "Marta", "seller", 0)
    s2 = await _mkuser(db, "potter@bazaar.local", "seller1234", "Tomas", "seller", 0)
    buyer = await _mkuser(db, "buyer@bazaar.local", "buyer1234", "June", "buyer", 20000)
    shops = {}
    for owner, name, blurb in [
        (s1, "Marta's Pantry", "Preserves, loaves and honey from the north road."),
        (s2, "Tomas & Clay", "Thrown bowls, mugs and plates. Dishwasher-brave."),
    ]:
        sid = await db.execute(
            "INSERT INTO shops (owner_id, name, blurb, created) VALUES (?, ?, ?, ?)",
            owner,
            name,
            blurb,
            _now(),
        )
        shops[owner] = int(sid)
    goods = [
        (s1, "Rye loaf", "Dark rye, baked Tuesday.", 450, 30, "Bakery"),
        (s1, "Plum jam", "Orchard plums, half sugar.", 650, 48, "Pantry"),
        (s1, "Wildflower honey", "Spring jars, 340g.", 900, 25, "Pantry"),
        (s1, "Pickled beets", "Sharp enough to wake you.", 500, 12, "Pantry"),
        (s2, "Speckled mug", "300ml, iron glaze.", 1800, 20, "Craft"),
        (s2, "Serving bowl", "Feeds four generously.", 3400, 8, "Craft"),
        (s2, "Espresso cups, pair", "Small, thick-walled.", 2200, 15, "Craft"),
        (s2, "Butter dish", "With lid. Obviously.", 2600, 0, "Craft"),
    ]
    pids = []
    for owner, name, detail, price, stock, cat in goods:
        pid = await db.execute(
            "INSERT INTO products (shop_id, name, detail, price_cents, stock, category, image, status, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            shops[owner],
            name,
            detail,
            price,
            stock,
            cat,
            "",
            "live",
            _now(),
        )
        pids.append(int(pid))
    # one newcomer awaiting moderation
    await db.execute(
        "INSERT INTO products (shop_id, name, detail, price_cents, stock, category, image, status, created)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        shops[s2],
        "Moon vase",
        "Tall neck, matte white.",
        4200,
        5,
        "Craft",
        "",
        "pending",
        _now(),
    )
    await db.execute(
        "INSERT INTO coupons (code, pct, max_uses, used, active) VALUES (?, ?, ?, ?, ?)",
        "WELCOME10",
        10,
        200,
        0,
        1,
    )
    # a finished order so reviews/tracking have something to show
    code = "BZ-DEMO01"
    await db.execute(
        "INSERT INTO orders (code, buyer_id, subtotal, discount, total, status, coupon, address, created)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        code,
        buyer,
        2450,
        0,
        2450,
        "delivered",
        "",
        "14 Mill Lane",
        (datetime.now() - timedelta(days=9)).isoformat(timespec="seconds"),
    )
    oid = (await db.fetch_one("SELECT id FROM orders WHERE code = ?", code))["id"]
    for pid, qty in [(pids[0], 1), (pids[4], 1)]:
        prow = await db.fetch_one("SELECT * FROM products WHERE id = ?", pid)
        await db.execute(
            "INSERT INTO order_items (order_id, product_id, shop_id, name, price_cents, qty)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            oid,
            pid,
            prow["shop_id"],
            prow["name"],
            prow["price_cents"],
            qty,
        )
    for st in ["placed", "paid", "packed", "shipped", "delivered"]:
        await db.execute("INSERT INTO order_events (order_id, status, at) VALUES (?, ?, ?)", oid, st, _now())
    await db.execute(
        "INSERT INTO reviews (product_id, user_id, name, stars, body, created) VALUES (?, ?, ?, ?, ?, ?)",
        pids[4],
        buyer,
        "June",
        5,
        "Survived a winter of dishwashers. Buying three more.",
        _now(),
    )
    await log(db, buyer, "seed", "June opened a tab at the square.")
    _ = admin, rng


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


async def need_role(req, *roles: str):
    """Session/JWT/key-agnostic RBAC: roles live on the user row.

    Raises LoginRequired (401/redirect) for strangers, Forbidden (403)
    for the wrong badge. Error says which badge opens the door.
    """
    uid = await current_user(req)
    me = await user_row(req, uid)
    if me["role"] not in roles:
        raise Forbidden(f"needs the {','.join(roles)} badge — you carry {me['role']}")
    return uid, me


# ---------------------------------------------------------------- html


def esc(s: object) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return f"{sign}${c // 100:,}.{c % 100:02d}"


def stars(avg: float, n: int = 0) -> str:
    full = int(round(avg)) if n else 0
    return "★★★★★"[:full] + "☆☆☆☆☆"[: 5 - full] + (f" <span class='muted'>({n})</span>" if n else "")


NAV = [
    ("market", "/", "Market"),
    ("orders", "/orders", "Orders"),
    ("sell", "/sell", "Sell"),
    ("wallet", "/wallet", "Wallet"),
    ("admin", "/admin", "Warden"),
    ("explorer", "/explorer", "Explorer"),
]


def layout(title: str, body: str, active: str = "", req=None, email: str | None = None) -> HTMLResponse:
    nav = "".join(
        f'<a href="{href}" class="{"on" if active == key else ""}">{label}</a>' for key, href, label in NAV
    )
    csrf = csrf_token(req) if req is not None else ""
    user = (
        f'<span class="who">{esc(email)}</span>'
        f'<form method="post" action="/logout" class="inline">'
        f'<input type="hidden" name="_csrf_token" value="{csrf}">'
        f'<button class="btn small ghost">Log out</button></form>'
        if email
        else '<a class="btn small" href="/login">Log in</a>'
    )
    flashes = ""
    if req is not None:
        msgs = get_flashed_messages(req)
        if msgs:
            flashes = (
                '<div class="flashes">' + "".join(f'<p class="flash">{esc(m)}</p>' for m in msgs) + "</div>"
            )
    search = (
        '<form method="get" action="/" class="search">'
        '<input name="q" placeholder="Search the square…" aria-label="Search">'
        '<button class="btn small ghost">→</button></form>'
        if email
        else ""
    )
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · BAZAAR</title>
<link rel="stylesheet" href="/static/style.css">
<link rel="icon" href="/static/favicon.svg"></head>
<body><div class="ticker" id="ticker"><span>BAZAAR WIRE</span><em id="wire">Stalls open. Fair prices. No haggling before noon.</em></div>
<div class="app"><aside><div class="brand"><span class="mark">❖</span> BAZAAR</div>
<p class="tag">Town square market</p><nav>{nav}</nav>
<div class="side-foot"><a href="/docs">API docs</a><a href="/openapi.json">OpenAPI</a>
<a href="/openapi.nish">Contract (NISH)</a>{user}</div></aside>
<main><div class="topbar"><h1>{esc(title)}</h1>{search}</div>{flashes}{body}</main></div>
<script src="/static/app.js" defer></script></body></html>"""
    )


def auth_page(kind: str, req, err: str = "", email: str = "") -> HTMLResponse:
    is_login = kind == "login"
    errh = f'<p class="err">{esc(err)}</p>' if err else ""
    name_field = (
        "" if is_login else '<label>Handle<input name="name" maxlength="60" placeholder="e.g. June"></label>'
    )
    swap = (
        'New to the square? <a href="/register">Open a tab</a> · '
        '<span class="muted">demo: buyer@bazaar.local / buyer1234</span>'
        if is_login
        else 'Have a tab? <a href="/login">Log in</a>'
    )
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{"Log in" if is_login else "Register"} · BAZAAR</title>
<link rel="stylesheet" href="/static/style.css"></head><body><div class="gate">
<div class="gate-card"><p class="kicker">BAZAAR · Town square market</p>
<h1>{"Back to the square" if is_login else "Open a tab"}</h1>{errh}
<form method="post" action="{"/login" if is_login else "/register"}" class="form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
{name_field}
<label>Email<input name="email" type="email" value="{esc(email)}" required></label>
<label>Password <small>{"welcome back" if is_login else "8+ characters · new tabs start with $200"}</small>
<input name="password" type="password" required></label>
<button>{"Log in" if is_login else "Open a tab"}</button></form>
<p class="muted">{swap}</p></div></div></body></html>"""
    )


def product_card(p: dict, avg: float = 0.0, n: int = 0) -> str:
    img = (
        f'<img src="/img/{p["id"]}" alt="" loading="lazy">'
        if p.get("image")
        else f'<div class="mono" style="--c:{CAT_INK.get(p["category"], "#8A8171")}">{esc((p["name"] or "?")[:1])}</div>'
    )
    stock = (
        '<span class="out">Sold out</span>'
        if not p["stock"]
        else (f'<span class="low">Only {p["stock"]} left</span>' if p["stock"] <= 3 else "")
    )
    return (
        f'<a class="prod" href="/p/{p["id"]}">{img}<b>{esc(p["name"])}</b>'
        f'<span class="muted">{esc(p["category"])}</span>'
        f'<span class="price">{money(p["price_cents"])}</span> {stock}'
        f'<span class="stars">{stars(avg, n)}</span></a>'
    )


def gmv_bars(days: list[dict]) -> str:
    mx = max([d["g"] for d in days] + [1])
    out = ['<svg viewBox="0 0 420 150" class="chart" role="img">']
    for i, d in enumerate(days):
        h = round(d["g"] / mx * 100) if mx else 0
        x = 8 + i * 29
        out.append(
            f'<rect x="{x}" y="{125 - h}" width="20" height="{max(h, 3)}" rx="2">'
            f"<title>{d['day']}: {money(d['g'])}</title></rect>"
        )
        if i % 2 == 0:
            out.append(f'<text x="{x + 10}" y="140" class="lbl">{d["day"][5:]}</text>')
    return "".join(out) + "</svg>"


# ---------------------------------------------------------------- checkout core


class CheckoutFailed(BadRequest):
    """The till says no: empty cart, short stock, thin wallet, dead coupon."""


async def run_checkout(db, uid: str, coupon_code: str, address: str) -> dict:
    """Wallet + stock + coupon + order, atomically.

    One transaction: either the whole till rings or nothing moves — no
    half-charged wallets, no negative stock. Idempotency-Key on the route
    above this stops double rings from retries.
    """
    coupon_code = (coupon_code or "").strip().upper()[:24]
    address = (address or "").strip()[:200]
    if not address:
        raise CheckoutFailed("the carrier needs an address — write one and retry")
    async with db.transaction():
        lines = await db.fetch_all(
            "SELECT c.qty, p.* FROM cart c JOIN products p ON p.id = c.product_id WHERE c.user_id = ?",
            uid,
        )
        if not lines:
            raise CheckoutFailed("cart is empty — put something in the basket first")
        for ln in lines:
            if ln["status"] != "live":
                raise CheckoutFailed(f"“{ln['name']}” is no longer on sale — drop it and retry")
            if ln["stock"] < ln["qty"]:
                raise CheckoutFailed(f"only {ln['stock']} × “{ln['name']}” left — lower the count and retry")
        subtotal = sum(ln["price_cents"] * ln["qty"] for ln in lines)
        discount, coupon = 0, ""
        if coupon_code:
            cp = await db.fetch_one("SELECT * FROM coupons WHERE code = ?", coupon_code)
            if not cp or not cp["active"]:
                raise CheckoutFailed(f"coupon {coupon_code} is unknown or retired")
            if cp["used"] >= cp["max_uses"]:
                raise CheckoutFailed(f"coupon {coupon_code} ran out of uses")
            discount = subtotal * cp["pct"] // 100
            coupon = coupon_code
            await db.execute("UPDATE coupons SET used = used + 1 WHERE code = ?", coupon_code)
        total = subtotal - discount
        me = await db.fetch_one("SELECT wallet_cents FROM users WHERE id = ?", uid)
        if me["wallet_cents"] < total:
            raise CheckoutFailed(
                f"tab covers {money(me['wallet_cents'])}, till wants {money(total)} — top up at /wallet"
            )
        await db.execute("UPDATE users SET wallet_cents = wallet_cents - ? WHERE id = ?", total, uid)
        for ln in lines:
            await db.execute("UPDATE products SET stock = stock - ? WHERE id = ?", ln["qty"], ln["id"])
        code = "BZ-" + secrets.token_hex(4).upper()
        oid = await db.execute(
            "INSERT INTO orders (code, buyer_id, subtotal, discount, total, status, coupon, address, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            code,
            uid,
            subtotal,
            discount,
            total,
            "paid",
            coupon,
            address,
            _now(),
        )
        for ln in lines:
            await db.execute(
                "INSERT INTO order_items (order_id, product_id, shop_id, name, price_cents, qty)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                oid,
                ln["id"],
                ln["shop_id"],
                ln["name"],
                ln["price_cents"],
                ln["qty"],
            )
        for st in ("placed", "paid"):
            await db.execute(
                "INSERT INTO order_events (order_id, status, at) VALUES (?, ?, ?)", oid, st, _now()
            )
        await db.execute("DELETE FROM cart WHERE user_id = ?", uid)
    await log(db, uid, "order", f"Order {code} rang at {money(total)}.")
    try:
        await TICKER.broadcast(
            {"sold": f"{sum(ln['qty'] for ln in lines)} goods", "order": code, "total_cents": total}
        )
    except Exception:
        pass
    return {"id": oid, "code": code, "total": total, "lines": len(lines)}


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
                "INSERT INTO users (id, email, pw, name, role, wallet_cents, created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                uid,
                data.email,
                hash_password(data.password),
                data.name or data.email.split("@")[0],
                "buyer",
                20000,
                _now(),
            )
        except Exception:
            if await req.app.state_db.fetch_one("SELECT id FROM users WHERE email = ?", data.email) is None:
                raise
            if is_json:
                raise BadRequest("email already has a tab — try /login instead")
            return auth_page("register", req, "That email has a tab already.", data.email)
        req.session["uid"] = uid
        bg.add(print, f"bazaar: tab opened for {data.email}")
        try:
            if hasattr(req.app, "state_queue"):
                await req.app.state_queue.enqueue(
                    "bazaar-receipt", {"code": "WELCOME", "buyer": data.email, "total": 0, "lines": 0}
                )
        except Exception:
            pass
        flash(req, "Tab opened with $200 house credit. Spend it wisely.")
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
        flash(req, f"Back to the square, {row['name']}.")
        if "text/html" in req.headers.get("accept", ""):
            return RedirectResponse("/", status_code=303)
        return {"uid": row["id"], "email": row["email"], "role": row["role"]}

    @app.post("/logout")
    async def logout(req):
        req.session.clear()
        ctype = req.headers.get("content-type", "")
        if "text/html" in req.headers.get("accept", "") or "multipart" in ctype:
            return RedirectResponse("/login", status_code=303)
        return {"ok": True}

    @app.get("/api/csrf")
    async def api_csrf(req):
        return {"csrf": csrf_token(req)}

    # ---- market home ----

    @app.get("/")
    async def home(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        db = req.app.state_db
        q = req.query.get("q", "").strip()[:80]
        cat = req.query.get("cat", "")
        sort = req.query.get("sort", "new")
        page = max(1, int(req.query.get("page", "1") or 1))
        where, args = ["p.status = 'live'"], []
        if q:
            where.append("(p.name LIKE ? OR p.detail LIKE ?)")
            args += [f"%{q}%", f"%{q}%"]
        if cat in CATS:
            where.append("p.category = ?")
            args.append(cat)
        w = " AND ".join(where)
        order = {
            "new": "p.id DESC",
            "price_asc": "p.price_cents ASC",
            "price_desc": "p.price_cents DESC",
            "rating": "avg_stars DESC",
            "popular": "sold DESC",
        }.get(sort, "p.id DESC")
        total = (await db.fetch_one(f"SELECT COUNT(*) AS n FROM products p WHERE {w}", *args))["n"]
        pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = min(page, pages)
        rows = await db.fetch_all(
            "SELECT p.*, COALESCE((SELECT AVG(stars) FROM reviews r WHERE r.product_id = p.id), 0) AS avg_stars,"
            " (SELECT COUNT(*) FROM reviews r WHERE r.product_id = p.id) AS n_rev,"
            " COALESCE((SELECT SUM(qty) FROM order_items o WHERE o.product_id = p.id), 0) AS sold"
            f" FROM products p WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
            *args,
            PER_PAGE,
            (page - 1) * PER_PAGE,
        )
        grid = "".join(product_card(dict(p), p["avg_stars"], p["n_rev"]) for p in rows) or (
            '<p class="muted">Nothing on the stalls matches. Try fewer words.</p>'
        )
        recent = await db.fetch_all(
            "SELECT o.name, o.qty FROM order_items o JOIN orders od ON od.id = o.order_id"
            " ORDER BY o.id DESC LIMIT 6"
        )
        wire = "".join(f"<li>Sold {r['qty']} × {esc(r['name'])}</li>" for r in recent)
        cats = "".join(f'<option value="{c}"{" selected" if cat == c else ""}>{c}</option>' for c in CATS)
        sorts = "".join(
            f'<option value="{v}"{" selected" if sort == v else ""}>{lbl}</option>'
            for v, lbl in [
                ("new", "Newest"),
                ("popular", "Most sold"),
                ("rating", "Top rated"),
                ("price_asc", "Price ↑"),
                ("price_desc", "Price ↓"),
            ]
        )
        prev = f'<a href="/?q={q}&cat={cat}&sort={sort}&page={page - 1}">← prev</a>' if page > 1 else ""
        nxt = f'<a href="/?q={q}&cat={cat}&sort={sort}&page={page + 1}">next →</a>' if page < pages else ""
        body = f"""
<form method="get" action="/" class="filters">
<input name="q" placeholder="Search the square…" value="{esc(q)}">
<select name="cat"><option value="">Every stall</option>{cats}</select>
<select name="sort">{sorts}</select><button class="small">Look</button></form>
<div class="grid-sq"><div class="catalog">{grid}</div>
<aside class="rail"><div class="panel"><h2>Fresh off the stalls</h2>
<ul class="plain" id="wirelist">{wire or '<li class="muted">Quiet morning.</li>'}</ul></div>
<div class="panel"><h2>House rules</h2>
<p class="muted">New tabs start with $200 credit. Coupons trim the till.
Sellers ship; the Warden moderates. The wire above is live.</p></div></aside></div>
<div class="pager">Page {page} of {pages} · {total} goods · {prev} {nxt}</div>"""
        return layout("Market", body, "market", req=req, email=me["email"])

    # ---- product page ----

    @app.get("/p/{pid:int}")
    async def product_page(req, pid: int, uid=Depends(current_user)):
        me = await user_row(req, uid)
        db = req.app.state_db
        p = await db.fetch_one(
            "SELECT p.*, s.name AS shop FROM products p JOIN shops s ON s.id = p.shop_id WHERE p.id = ?", pid
        )
        if not p or (p["status"] != "live" and me["role"] != "admin"):
            raise NotFound(f"no goods #{pid} on sale — the square shows live stalls only")
        agg = await db.fetch_one(
            "SELECT AVG(stars) AS a, COUNT(*) AS n FROM reviews WHERE product_id = ?", pid
        )
        avg, n = agg["a"] or 0, agg["n"]
        revs = await db.fetch_all("SELECT * FROM reviews WHERE product_id = ? ORDER BY id DESC", pid)
        rev_html = (
            "".join(
                f"<li><b>{'★' * r['stars']}{'☆' * (5 - r['stars'])}</b> {esc(r['name'])}"
                f' <span class="muted">{esc(r["created"][:10])}</span><br>{esc(r["body"] or "")}</li>'
                for r in revs
            )
            or '<li class="muted">No verdicts yet. Buy it and judge.</li>'
        )
        bought = await db.fetch_one(
            "SELECT 1 AS ok FROM order_items o JOIN orders od ON od.id = o.order_id"
            " WHERE od.buyer_id = ? AND o.product_id = ? AND od.status != 'cancelled' LIMIT 1",
            uid,
            pid,
        )
        mine = await db.fetch_one("SELECT id FROM reviews WHERE product_id = ? AND user_id = ?", pid, uid)
        review_form = ""
        if bought and not mine:
            review_form = f"""<form method="post" action="/p/{pid}/review" class="form inline-form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<select name="stars"><option value="5">★★★★★</option><option value="4">★★★★</option>
<option value="3">★★★</option><option value="2">★★</option><option value="1">★</option></select>
<input name="body" maxlength="2000" placeholder="Your verdict…"><button class="small">Judge it</button></form>"""
        wished = await db.fetch_one("SELECT 1 FROM wishlist WHERE user_id = ? AND product_id = ?", uid, pid)
        img = (
            f'<img class="hero" src="/img/{p["id"]}" alt="">'
            if p["image"]
            else f'<div class="hero mono" style="--c:{CAT_INK.get(p["category"], "#8A8171")}">{esc(p["name"][:1])}</div>'
        )
        body = f"""<p><a href="/">← the square</a></p>
<div class="grid"><div class="panel">{img}
<p class="kicker">{esc(p["category"])} · from {esc(p["shop"])}</p>
<p class="price big">{money(p["price_cents"])}</p>
<p class="stars">{stars(avg, n)}</p>
<p>{esc(p["detail"] or "No story told.")}</p>
<p class="muted">{"Sold out" if not p["stock"] else f"{p['stock']} on the shelf"}</p>
<div class="rowbtns">
<form method="post" action="/cart/add" class="inline">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<input type="hidden" name="pid" value="{pid}">
<input type="hidden" name="qty" value="1"><button {"disabled" if not p["stock"] else ""}>Add to basket</button></form>
<form method="post" action="/wishlist" class="inline">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<input type="hidden" name="pid" value="{pid}">
<button class="ghost">{"★ Saved" if wished else "☆ Save"}</button></form></div></div>
<div class="panel"><h2>Verdicts · {n}</h2><ul class="plain">{rev_html}</ul>{review_form}</div></div>"""
        return layout(p["name"], body, "market", req=req, email=me["email"])

    @app.get("/img/{pid:int}")
    async def product_img(req, pid: int):
        row = await req.app.state_db.fetch_one("SELECT image FROM products WHERE id = ?", pid)
        if not row or not row["image"]:
            raise NotFound("no portrait for these goods")
        path = UPLOADS / row["image"]
        if not path.is_file():
            raise NotFound("the portrait is gone — the stall keeps a monogram instead")
        return FileResponse(str(path))

    @app.post("/p/{pid:int}/review")
    async def review_add(req, pid: int, uid=Depends(current_user)):
        me = await user_row(req, uid)
        db = req.app.state_db
        if await db.fetch_one("SELECT id FROM products WHERE id = ?", pid) is None:
            raise NotFound(f"no goods #{pid}")
        bought = await db.fetch_one(
            "SELECT 1 AS ok FROM order_items o JOIN orders od ON od.id = o.order_id"
            " WHERE od.buyer_id = ? AND o.product_id = ? AND od.status != 'cancelled' LIMIT 1",
            uid,
            pid,
        )
        if not bought:
            raise Forbidden("verdicts come from buyers — buy it first, then judge")
        form = await req.form()
        try:
            data = ReviewIn.validate({"stars": form.get("stars", "5"), "body": form.get("body", "")})
        except Exception as e:
            raise BadRequest(f"fix the verdict and retry: {e}")
        try:
            await db.execute(
                "INSERT INTO reviews (product_id, user_id, name, stars, body, created)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                pid,
                uid,
                me["name"],
                int(data.stars),
                data.body,
                _now(),
            )
        except Exception:
            raise BadRequest("one verdict per buyer — yours is already pinned up")
        await CACHE.delete("metrics")
        flash(req, "Verdict pinned. The stall thanks you.")
        return RedirectResponse(f"/p/{pid}", status_code=303)

    @app.post("/wishlist")
    async def wishlist_toggle(req, uid=Depends(current_user)):
        form = await req.form()
        try:
            pid = int(form.get("pid", "0"))
        except ValueError:
            raise BadRequest("which goods? pass a pid")
        if await req.app.state_db.fetch_one("SELECT id FROM products WHERE id = ?", pid) is None:
            raise NotFound(f"no goods #{pid}")
        hit = await req.app.state_db.fetch_one(
            "SELECT 1 FROM wishlist WHERE user_id = ? AND product_id = ?", uid, pid
        )
        if hit:
            await req.app.state_db.execute(
                "DELETE FROM wishlist WHERE user_id = ? AND product_id = ?", uid, pid
            )
        else:
            await req.app.state_db.execute(
                "INSERT INTO wishlist (user_id, product_id) VALUES (?, ?)", uid, pid
            )
        back = req.headers.get("referer", f"/p/{pid}")
        return RedirectResponse(back if back.startswith("/") else f"/p/{pid}", status_code=303)

    # ---- cart + checkout ----

    async def _cart_lines(db, uid: str) -> list[dict]:
        return await db.fetch_all(
            "SELECT c.qty, p.* FROM cart c JOIN products p ON p.id = c.product_id WHERE c.user_id = ?", uid
        )

    @app.get("/cart")
    async def cart_page(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        lines = await _cart_lines(req.app.state_db, uid)
        rows = (
            "".join(
                f'<tr><td><a href="/p/{ln["id"]}">{esc(ln["name"])}</a></td>'
                f'<td class="r">{money(ln["price_cents"])}</td>'
                f'<td><form method="post" action="/cart/set" class="inline">'
                f'<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">'
                f'<input type="hidden" name="pid" value="{ln["id"]}">'
                f'<input type="number" name="qty" value="{ln["qty"]}" min="0" max="{ln["stock"]}" class="qty">'
                f'<button class="mini">set</button></form></td>'
                f'<td class="r">{money(ln["price_cents"] * ln["qty"])}</td></tr>'
                for ln in lines
            )
            or '<tr><td colspan="4" class="muted">Basket is empty. The square awaits.</td></tr>'
        )
        sub = sum(ln["price_cents"] * ln["qty"] for ln in lines)
        body = f"""<div class="panel"><table>{rows}</table>
<p class="total">Subtotal <b>{money(sub)}</b> · tab holds <b>{money(me["wallet_cents"])}</b></p>
<form method="post" action="/checkout" class="form inline-form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<input name="coupon" maxlength="24" placeholder="Coupon code">
<input name="address" required maxlength="200" placeholder="Deliver to…">
<button>Ring the till (idempotent)</button></form>
<p class="muted">Retries with the same <code>Idempotency-Key</code> replay — never double-ring.</p></div>"""
        return layout("Basket", body, "market", req=req, email=me["email"])

    @app.post("/cart/add")
    async def cart_add(req, uid=Depends(current_user)):
        form = await req.form()
        try:
            pid, qty = int(form.get("pid", "0")), max(1, int(form.get("qty", "1")))
        except ValueError:
            raise BadRequest("qty must be a whole number — 1, 2, 3…")
        p = await req.app.state_db.fetch_one("SELECT * FROM products WHERE id = ?", pid)
        if not p or p["status"] != "live":
            raise NotFound(f"no goods #{pid} on sale")
        if p["stock"] < 1:
            raise CheckoutFailed(f"“{p['name']}” is sold out")
        qty = min(qty, p["stock"])
        hit = await req.app.state_db.fetch_one(
            "SELECT qty FROM cart WHERE user_id = ? AND product_id = ?", uid, pid
        )
        if hit:
            await req.app.state_db.execute(
                "UPDATE cart SET qty = ?, added = ? WHERE user_id = ? AND product_id = ?",
                min(hit["qty"] + qty, p["stock"]),
                _now(),
                uid,
                pid,
            )
        else:
            await req.app.state_db.execute(
                "INSERT INTO cart (user_id, product_id, qty, added) VALUES (?, ?, ?, ?)",
                uid,
                pid,
                qty,
                _now(),
            )
        return RedirectResponse("/cart", status_code=303)

    @app.post("/cart/set")
    async def cart_set(req, uid=Depends(current_user)):
        form = await req.form()
        try:
            pid, qty = int(form.get("pid", "0")), max(0, int(form.get("qty", "0")))
        except ValueError:
            raise BadRequest("qty must be a whole number")
        if qty == 0:
            await req.app.state_db.execute("DELETE FROM cart WHERE user_id = ? AND product_id = ?", uid, pid)
        else:
            p = await req.app.state_db.fetch_one("SELECT stock FROM products WHERE id = ?", pid)
            if not p:
                raise NotFound(f"no goods #{pid}")
            await req.app.state_db.execute(
                "UPDATE cart SET qty = ? WHERE user_id = ? AND product_id = ?",
                min(qty, p["stock"]),
                uid,
                pid,
            )
        return RedirectResponse("/cart", status_code=303)

    @app.post("/checkout")
    async def checkout(req, uid=Depends(current_user)):
        form = await req.form()
        try:
            order = await run_checkout(req.app.state_db, uid, form.get("coupon", ""), form.get("address", ""))
        except CheckoutFailed as e:
            raise e
        try:
            if hasattr(req.app, "state_queue"):
                await req.app.state_queue.enqueue(
                    "bazaar-receipt",
                    {
                        "code": order["code"],
                        "buyer": uid,
                        "total": order["total"] / 100,
                        "lines": order["lines"],
                    },
                )
        except Exception:
            pass
        flash(req, f"Order {order['code']} paid at {money(order['total'])}. The stalls are packing.")
        return RedirectResponse(f"/orders/{order['code']}", status_code=303)

    # ---- orders ----

    @app.get("/orders")
    async def orders_page(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        db = req.app.state_db
        if me["role"] == "admin":
            rows = await db.fetch_all("SELECT * FROM orders ORDER BY id DESC LIMIT 60")
        elif me["role"] == "seller":
            shop = await db.fetch_one("SELECT id FROM shops WHERE owner_id = ?", uid)
            rows = (
                await db.fetch_all(
                    "SELECT DISTINCT o.* FROM orders o JOIN order_items i ON i.order_id = o.id"
                    " WHERE i.shop_id = ? ORDER BY o.id DESC LIMIT 60",
                    shop["id"],
                )
                if shop
                else []
            )
        else:
            rows = await db.fetch_all("SELECT * FROM orders WHERE buyer_id = ? ORDER BY id DESC", uid)
        items = (
            "".join(
                f'<tr><td><a href="/orders/{r["code"]}">{esc(r["code"])}</a></td>'
                f'<td><span class="pill">{esc(r["status"])}</span></td>'
                f'<td class="r">{money(r["total"])}</td><td class="d">{esc(r["created"][:10])}</td></tr>'
                for r in rows
            )
            or '<tr><td colspan="4" class="muted">No orders yet.</td></tr>'
        )
        return layout(
            "Orders", f'<div class="panel"><table>{items}</table></div>', "orders", req=req, email=me["email"]
        )

    async def _order_for(db, code: str, uid: str, role: str):
        o = await db.fetch_one("SELECT * FROM orders WHERE code = ?", code)
        if not o:
            raise NotFound(f"no order {code} — check /orders")
        if role == "buyer" and o["buyer_id"] != uid:
            raise Forbidden("that parcel is addressed to someone else")
        if role == "seller":
            shop = await db.fetch_one("SELECT id FROM shops WHERE owner_id = ?", uid)
            mine = (
                await db.fetch_one(
                    "SELECT 1 AS ok FROM order_items WHERE order_id = ? AND shop_id = ?", o["id"], shop["id"]
                )
                if shop
                else None
            )
            if not mine:
                raise Forbidden("no goods of yours in that parcel")
        return o

    @app.get("/orders/{code}")
    async def order_page(req, code: str, uid=Depends(current_user)):
        me = await user_row(req, uid)
        db = req.app.state_db
        o = await _order_for(db, code, uid, me["role"])
        lines = await db.fetch_all("SELECT * FROM order_items WHERE order_id = ?", o["id"])
        evs = await db.fetch_all("SELECT * FROM order_events WHERE order_id = ? ORDER BY id", o["id"])
        items = "".join(
            f'<tr><td>{esc(ln["name"])}</td><td class="r">{money(ln["price_cents"])} × {ln["qty"]}</td>'
            f'<td class="r">{money(ln["price_cents"] * ln["qty"])}</td></tr>'
            for ln in lines
        )
        trail = "".join(
            f"<li><span class='pill'>{esc(e['status'])}</span> <span class='muted'>{esc(e['at'])}</span></li>"
            for e in evs
        )
        coupon_bit = f"· coupon {esc(o['coupon'])}" if o["coupon"] else ""
        actions = ""
        if me["role"] in ("seller", "admin") and o["status"] in NEXT:
            nxt = NEXT[o["status"]]
            actions += f"""<form method="post" action="/orders/{code}/advance" class="inline">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<button class="small">Mark {nxt}</button></form>"""
        if me["role"] in ("buyer", "admin") and o["status"] in ("placed", "paid"):
            actions += f"""<form method="post" action="/orders/{code}/cancel" class="inline">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<button class="small danger">Cancel + refund</button></form>"""
        body = f"""<p class="kicker">ORDER {esc(o["code"])} · {esc(o["status"]).upper()}</p>
<div class="grid"><div class="panel"><table>{items}</table>
<p class="total">Subtotal {money(o["subtotal"])} · coupon −{money(o["discount"])} ·
Total <b>{money(o["total"])}</b></p>
<p class="muted">To: {esc(o["address"])} {coupon_bit}</p>
<p class="rowbtns">{actions}</p></div>
<div class="panel"><h2>Trail</h2><ul class="plain">{trail}</ul></div></div>"""
        return layout(f"Order {o['code']}", body, "orders", req=req, email=me["email"])

    @app.post("/orders/{code}/advance")
    async def order_advance(req, code: str, uid=Depends(current_user)):
        me = await user_row(req, uid)
        if me["role"] not in ("seller", "admin"):
            raise Forbidden("only stalls and the Warden move parcels — you carry " + me["role"])
        db = req.app.state_db
        o = await _order_for(db, code, uid, me["role"])
        if o["status"] not in NEXT:
            raise BadRequest(f"order {code} is {o['status']} — nowhere left to move it")
        nxt = NEXT[o["status"]]
        await db.execute("UPDATE orders SET status = ? WHERE id = ?", nxt, o["id"])
        await db.execute(
            "INSERT INTO order_events (order_id, status, at) VALUES (?, ?, ?)", o["id"], nxt, _now()
        )
        await CACHE.delete("metrics")
        flash(req, f"Order {code} is now {nxt}.")
        return RedirectResponse(f"/orders/{code}", status_code=303)

    @app.post("/orders/{code}/cancel")
    async def order_cancel(req, code: str, uid=Depends(current_user)):
        me = await user_row(req, uid)
        db = req.app.state_db
        o = await _order_for(db, code, uid, me["role"] if me["role"] != "admin" else "admin")
        if me["role"] == "buyer" and o["buyer_id"] != uid:
            raise Forbidden("that parcel is addressed to someone else")
        if o["status"] not in ("placed", "paid"):
            raise BadRequest(f"order {code} is already {o['status']} — past cancelling")
        async with db.transaction():
            await db.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", o["id"])
            await db.execute(
                "INSERT INTO order_events (order_id, status, at) VALUES (?, ?, ?)",
                o["id"],
                "cancelled",
                _now(),
            )
            await db.execute(
                "UPDATE users SET wallet_cents = wallet_cents + ? WHERE id = ?", o["total"], o["buyer_id"]
            )
            for ln in await db.fetch_all(
                "SELECT product_id, qty FROM order_items WHERE order_id = ?", o["id"]
            ):
                await db.execute(
                    "UPDATE products SET stock = stock + ? WHERE id = ?", ln["qty"], ln["product_id"]
                )
        await CACHE.delete("metrics")
        flash(req, f"Order {code} cancelled — {money(o['total'])} back on your tab.")
        return RedirectResponse(f"/orders/{code}", status_code=303)

    # ---- wallet ----

    @app.get("/wallet")
    async def wallet_page(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        body = f"""<div class="cards"><div class="card"><span>House tab</span><b>{money(me["wallet_cents"])}</b>
<em>good at every stall</em></div></div>
<div class="panel"><h2>Top up (play money)</h2>
<form method="post" action="/wallet/topup" class="form inline-form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<input name="amount" required placeholder="25.00"><button class="small">Add funds</button></form>
<p class="muted">The house bank asks no questions.</p></div>"""
        return layout("Wallet", body, "wallet", req=req, email=me["email"])

    @app.post("/wallet/topup")
    async def wallet_topup(req, uid=Depends(current_user)):
        form = await req.form()
        try:
            data = TopupIn.validate({"amount": form.get("amount", "")})
        except Exception as e:
            raise BadRequest(f"fix the amount and retry: {e}")
        await req.app.state_db.execute(
            "UPDATE users SET wallet_cents = wallet_cents + ? WHERE id = ?",
            int(round(float(data.amount) * 100)),
            uid,
        )
        flash(req, f"{money(int(round(float(data.amount) * 100)))} added to your tab.")
        return RedirectResponse("/wallet", status_code=303)

    # ---- seller desk ----

    @app.get("/sell")
    async def sell_desk(req, uid=Depends(current_user)):
        uid, me = await need_role(req, "seller", "admin")
        db = req.app.state_db
        shop = await db.fetch_one("SELECT * FROM shops WHERE owner_id = ?", uid)
        if not shop and me["role"] != "admin":
            body = f"""<div class="panel"><h2>Open a stall</h2>
<form method="post" action="/sell/shop" class="form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<label>Stall name<input name="name" required maxlength="80"></label>
<label>Signboard<textarea name="blurb" rows="2" maxlength="300"></textarea></label>
<button>Raise the sign</button></form></div>"""
            return layout("Sell", body, "sell", req=req, email=me["email"])
        sid = shop["id"] if shop else None
        filt = "1 = 1" if me["role"] == "admin" else f"p.shop_id = {int(sid)}"
        mine = await db.fetch_all(
            "SELECT p.*, COALESCE((SELECT SUM(qty) FROM order_items o WHERE o.product_id = p.id),0) AS sold"
            f" FROM products p WHERE {filt} ORDER BY p.id DESC"
        )
        open_orders = await _open_orders(db, sid, me["role"])
        days = []
        for d in range(13, -1, -1):
            day = (date.today() - timedelta(days=d)).isoformat()
            g = await db.fetch_one(
                "SELECT COALESCE(SUM(i.price_cents * i.qty),0) AS g FROM order_items i JOIN orders o ON o.id = i.order_id"
                f" WHERE o.status != 'cancelled' AND substr(o.created,1,10) = ? AND (1 = 1 {'AND i.shop_id = ' + str(int(sid)) if sid else ''})",
                day,
            )
            days.append({"day": day, "g": g["g"]})
        rows = (
            "".join(
                f'<tr><td><b>{esc(p["name"])}</b><br><span class="muted">{esc(p["category"])} · sold {p["sold"]}</span></td>'
                f'<td class="r">{money(p["price_cents"])}</td><td class="r">{p["stock"]}</td>'
                f'<td><span class="pill">{esc(p["status"])}</span></td>'
                f'<td><a href="/sell/products/{p["id"]}/edit">edit</a></td></tr>'
                for p in mine
            )
            or '<tr><td colspan="5" class="muted">Empty stall.</td></tr>'
        )
        oo = (
            "".join(
                f'<li><a href="/orders/{r["code"]}">{esc(r["code"])}</a> · {esc(r["status"])} · {money(r["total"])}</li>'
                for r in open_orders
            )
            or '<li class="muted">Nothing to pack.</li>'
        )
        body = f"""
<p class="kicker">{"WARDEN VIEW" if me["role"] == "admin" and not shop else "STALL · " + esc(shop["name"] if shop else "all")}</p>
<div class="grid"><div class="panel"><h2>Takings · 14 days</h2>{gmv_bars(days)}</div>
<div class="panel"><h2>To pack</h2><ul class="plain">{oo}</ul></div></div>
<p><a class="btn" href="/sell/products/new">+ New goods</a></p>
<div class="panel"><table>{rows}</table></div>"""
        return layout("Seller desk", body, "sell", req=req, email=me["email"])

    async def _open_orders(db, sid, role):
        if role == "admin":
            return await db.fetch_all(
                "SELECT code, status, total FROM orders WHERE status IN ('placed','paid','packed')"
                " ORDER BY id DESC LIMIT 10"
            )
        if not sid:
            return []
        return await db.fetch_all(
            "SELECT DISTINCT o.code, o.status, o.total FROM orders o JOIN order_items i ON i.order_id = o.id"
            " WHERE o.status IN ('placed','paid','packed') AND i.shop_id = ? ORDER BY o.id DESC LIMIT 10",
            sid,
        )

    @app.post("/sell/shop")
    async def shop_open(req, uid=Depends(current_user)):
        uid, me = await need_role(req, "seller", "admin", "buyer")
        if me["role"] == "buyer":
            await req.app.state_db.execute("UPDATE users SET role = 'seller' WHERE id = ?", uid)
        if await req.app.state_db.fetch_one("SELECT id FROM shops WHERE owner_id = ?", uid):
            return RedirectResponse("/sell", status_code=303)
        form = await req.form()
        try:
            data = ShopIn.validate({"name": form.get("name", ""), "blurb": form.get("blurb", "")})
        except Exception as e:
            raise BadRequest(f"fix the sign and retry: {e}")
        await req.app.state_db.execute(
            "INSERT INTO shops (owner_id, name, blurb, created) VALUES (?, ?, ?, ?)",
            uid,
            data.name,
            data.blurb,
            _now(),
        )
        flash(req, f"“{data.name}” is raised. Stock it.")
        return RedirectResponse("/sell", status_code=303)

    @app.get("/sell/products/new")
    async def product_new(req, uid=Depends(current_user)):
        await need_role(req, "seller", "admin")
        me = await user_row(req, uid)
        opts = "".join(f"<option>{c}</option>" for c in CATS)
        body = f"""<div class="panel"><form method="post" action="/sell/products" enctype="multipart/form-data" class="form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<label>Name<input name="name" required maxlength="120"></label>
<label>Story<textarea name="detail" rows="3" maxlength="2000"></textarea></label>
<div class="frow"><label>Price $<input name="price" required placeholder="12.50"></label>
<label>Stock<input name="stock" value="10"></label></div>
<label>Shelf<select name="category">{opts}</select></label>
<label>Portrait <small>up to 4 MB</small><input type="file" name="image"></label>
<button>Put on the back shelf (draft)</button></form></div>"""
        return layout("New goods", body, "sell", req=req, email=me["email"])

    async def _read_product_form(req, max_img: int):
        form = await req.form(max_file_size=max_img)
        try:
            data = ProductIn.validate(
                {
                    "name": form.get("name", ""),
                    "detail": form.get("detail", ""),
                    "price": form.get("price", ""),
                    "stock": form.get("stock", "0"),
                    "category": form.get("category", "Other"),
                }
            )
        except Exception as e:
            raise BadRequest(f"fix the goods and retry: {e}")
        stored = ""
        f = form.get("image")
        if isinstance(f, UploadFile) and f.filename:
            raw = await f.read()
            if raw:
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", f.filename)[-60:] or "img"
                stored = f"{secrets.token_hex(6)}_{safe}"
                (UPLOADS / stored).write_bytes(raw)
        return data, stored

    @app.post("/sell/products")
    async def product_create(req, uid=Depends(current_user)):
        uid, me = await need_role(req, "seller", "admin")
        shop = await req.app.state_db.fetch_one("SELECT id FROM shops WHERE owner_id = ?", uid)
        if not shop and me["role"] != "admin":
            raise Forbidden("raise a stall first — /sell walks you through it")
        data, stored = await _read_product_form(req, 4 * 1024 * 1024)
        pid = await req.app.state_db.execute(
            "INSERT INTO products (shop_id, name, detail, price_cents, stock, category, image, status, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            shop["id"] if shop else None,
            data.name,
            data.detail,
            int(round(float(data.price) * 100)),
            int(data.stock),
            data.category if data.category in CATS else "Other",
            stored,
            "draft",
            _now(),
        )
        flash(req, f"“{data.name}” is a draft. Submit it for the Warden's chalk.")
        return RedirectResponse(f"/sell/products/{pid}/edit", status_code=303)

    @app.get("/sell/products/{pid:int}/edit")
    async def product_edit_page(req, pid: int, uid=Depends(current_user)):
        uid, me = await need_role(req, "seller", "admin")
        p = await req.app.state_db.fetch_one("SELECT * FROM products WHERE id = ?", pid)
        if not p:
            raise NotFound(f"no goods #{pid}")
        await _own_or_admin(req, uid, me, p)
        opts = "".join(f"<option{' selected' if p['category'] == c else ''}>{c}</option>" for c in CATS)
        body = f"""<p class="kicker">STATUS · {esc(p["status"]).upper()}</p>
<div class="panel"><form method="post" action="/sell/products/{pid}/edit" enctype="multipart/form-data" class="form">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<label>Name<input name="name" value="{esc(p["name"])}" required maxlength="120"></label>
<label>Story<textarea name="detail" rows="3" maxlength="2000">{esc(p["detail"])}</textarea></label>
<div class="frow"><label>Price $<input name="price" value="{p["price_cents"] / 100:.2f}" required></label>
<label>Stock<input name="stock" value="{p["stock"]}"></label></div>
<label>Shelf<select name="category">{opts}</select></label>
<label>New portrait <small>optional</small><input type="file" name="image"></label>
<button class="small">Save</button></form>
<div class="rowbtns" style="margin-top:10px">
<form method="post" action="/sell/products/{pid}/submit" class="inline">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<button class="small">Submit for review</button></form></div></div>"""
        return layout(f"Edit · {p['name']}", body, "sell", req=req, email=me["email"])

    async def _own_or_admin(req, uid: str, me: dict, p: dict) -> None:
        if me["role"] == "admin":
            return
        shop = await req.app.state_db.fetch_one("SELECT id FROM shops WHERE owner_id = ?", uid)
        if not shop or p["shop_id"] != shop["id"]:
            raise Forbidden("those goods belong to another stall")

    @app.post("/sell/products/{pid:int}/edit")
    async def product_edit(req, pid: int, uid=Depends(current_user)):
        uid, me = await need_role(req, "seller", "admin")
        p = await req.app.state_db.fetch_one("SELECT * FROM products WHERE id = ?", pid)
        if not p:
            raise NotFound(f"no goods #{pid}")
        await _own_or_admin(req, uid, me, p)
        data, stored = await _read_product_form(req, 4 * 1024 * 1024)
        await req.app.state_db.execute(
            "UPDATE products SET name=?, detail=?, price_cents=?, stock=?, category=?, image=COALESCE(NULLIF(?, ''), image)"
            " WHERE id = ?",
            data.name,
            data.detail,
            int(round(float(data.price) * 100)),
            int(data.stock),
            data.category if data.category in CATS else "Other",
            stored,
            pid,
        )
        return RedirectResponse(f"/sell/products/{pid}/edit", status_code=303)

    @app.post("/sell/products/{pid:int}/submit")
    async def product_submit(req, pid: int, uid=Depends(current_user)):
        uid, me = await need_role(req, "seller", "admin")
        p = await req.app.state_db.fetch_one("SELECT * FROM products WHERE id = ?", pid)
        if not p:
            raise NotFound(f"no goods #{pid}")
        await _own_or_admin(req, uid, me, p)
        await req.app.state_db.execute("UPDATE products SET status = 'pending' WHERE id = ?", pid)
        flash(req, "On the Warden's desk. Live soon, if it passes.")
        return RedirectResponse("/sell", status_code=303)

    # ---- warden (admin) ----

    @app.get("/admin")
    async def admin_page(req, uid=Depends(current_user)):
        uid, me = await need_role(req, "admin")
        db = req.app.state_db
        pending = await db.fetch_all(
            "SELECT p.*, s.name AS shop FROM products p LEFT JOIN shops s ON s.id = p.shop_id"
            " WHERE p.status = 'pending' ORDER BY p.id"
        )
        prow = (
            "".join(
                f'<tr><td><b>{esc(p["name"])}</b><br><span class="muted">{esc(p["shop"] or "no stall")} ·'
                f" {money(p['price_cents'])} · stock {p['stock']}</span></td>"
                f"<td>{esc((p['detail'] or '')[:90])}</td>"
                f'<td><form method="post" action="/admin/products/{p["id"]}/approve" class="inline">'
                f'<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">'
                f'<button class="mini">chalk ✓</button></form> '
                f'<form method="post" action="/admin/products/{p["id"]}/reject" class="inline">'
                f'<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">'
                f'<button class="mini danger">chalk ×</button></form></td></tr>'
                for p in pending
            )
            or '<tr><td colspan="3" class="muted">Desk is clear.</td></tr>'
        )
        coupons = await db.fetch_all("SELECT * FROM coupons ORDER BY id DESC")
        crow = (
            "".join(
                f"<li><code>{esc(c['code'])}</code> −{c['pct']}% · {c['used']}/{c['max_uses']} used ·"
                f" {'live' if c['active'] else 'retired'}</li>"
                for c in coupons
            )
            or '<li class="muted">No coupons cut.</li>'
        )
        users = await db.fetch_all("SELECT id, name, email, role FROM users ORDER BY created")
        urow = "".join(
            f'<tr><td><b>{esc(u["name"])}</b><br><span class="muted">{esc(u["email"])}</span></td>'
            f'<td><span class="pill">{esc(u["role"])}</span></td>'
            f'<td><form method="post" action="/admin/users/{u["id"]}/role" class="inline">'
            f'<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">'
            f'<select name="role">{"".join("<option" + (" selected" if u["role"] == r else "") + f">{r}</option>" for r in ("buyer", "seller", "admin"))}</select>'
            f'<button class="mini">set</button></form></td></tr>'
            for u in users
        )
        body = f"""<div class="panel"><h2>Moderation · {len(pending)} awaiting chalk</h2>
<table>{prow}</table></div>
<div class="grid"><div class="panel"><h2>Coupons</h2><ul class="plain">{crow}</ul>
<form method="post" action="/admin/coupons" class="form inline-form" style="margin-top:10px">
<input type="hidden" name="_csrf_token" value="{csrf_token(req)}">
<input name="code" maxlength="24" placeholder="CODE" style="text-transform:uppercase">
<input name="pct" placeholder="% off"><input name="max_uses" placeholder="uses">
<button class="small">Cut coupon</button></form></div>
<div class="panel"><h2>Badges</h2><table>{urow}</table></div></div>"""
        return layout("Warden's desk", body, "admin", req=req, email=me["email"])

    @app.post("/admin/products/{pid:int}/approve")
    async def product_approve(req, pid: int, uid=Depends(current_user)):
        await need_role(req, "admin")
        if await req.app.state_db.fetch_one("SELECT id FROM products WHERE id = ?", pid) is None:
            raise NotFound(f"no goods #{pid}")
        await req.app.state_db.execute("UPDATE products SET status = 'live' WHERE id = ?", pid)
        await CACHE.delete("metrics")
        flash(req, f"Goods #{pid} chalked live.")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/products/{pid:int}/reject")
    async def product_reject(req, pid: int, uid=Depends(current_user)):
        await need_role(req, "admin")
        if await req.app.state_db.fetch_one("SELECT id FROM products WHERE id = ?", pid) is None:
            raise NotFound(f"no goods #{pid}")
        await req.app.state_db.execute("UPDATE products SET status = 'rejected' WHERE id = ?", pid)
        flash(req, f"Goods #{pid} sent back.")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/coupons")
    async def coupon_create(req, uid=Depends(current_user)):
        await need_role(req, "admin")
        form = await req.form()
        try:
            data = CouponIn.validate(
                {
                    "code": (form.get("code", "") or "").upper(),
                    "pct": form.get("pct", ""),
                    "max_uses": form.get("max_uses", ""),
                }
            )
        except Exception as e:
            raise BadRequest(f"fix the coupon and retry: {e}")
        try:
            await req.app.state_db.execute(
                "INSERT INTO coupons (code, pct, max_uses, used, active) VALUES (?, ?, ?, ?, ?)",
                data.code,
                int(data.pct),
                int(data.max_uses),
                0,
                1,
            )
        except Exception:
            raise BadRequest(f"coupon {data.code} already exists — cut another code")
        flash(req, f"Coupon {data.code} cut at {data.pct}% off.")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/users/{who}/role")
    async def role_set(req, who: str, uid=Depends(current_user)):
        await need_role(req, "admin")
        form = await req.form()
        try:
            data = RoleIn.validate({"role": form.get("role", "")})
        except Exception as e:
            raise BadRequest(f"badges come in buyer/seller/admin only: {e}")
        if who == uid and data.role != "admin":
            raise BadRequest("the Warden cannot unbade themselves — ask another Warden")
        await req.app.state_db.execute("UPDATE users SET role = ? WHERE id = ?", data.role, who)
        return RedirectResponse("/admin", status_code=303)

    @app.get("/export/orders.csv")
    async def export_orders(req, uid=Depends(current_user)):
        await need_role(req, "admin")
        rows = await req.app.state_db.fetch_all(
            "SELECT code, buyer_id, total, status, created FROM orders ORDER BY id"
        )

        def gen():
            yield "code,buyer,total_cents,status,created\n"
            for r in rows:
                yield f"{r['code']},{r['buyer_id']},{r['total']},{r['status']},{r['created']}\n"

        resp = StreamingResponse(gen(), media_type="text/csv")
        resp.headers["content-disposition"] = "attachment; filename=bazaar-orders.csv"
        return resp

    # ---- explorer ----

    @app.get("/explorer")
    async def explorer_page(req, uid=Depends(current_user)):
        me = await user_row(req, uid)
        rows = [
            ("GET", "/api/products?q=rye", "Catalog search, filters, sort, pages"),
            ("GET", "/api/products/1", "One goods with rating + stock"),
            ("GET", "/api/cart", "Your basket"),
            ("POST", "/api/checkout", "Atomic till — JSON or NISH body"),
            ("GET", "/api/orders", "Your parcels (role-scoped)"),
            ("GET", "/api/metrics", "GMV, pipeline, top goods, per-stall rollup"),
            ("GET", "/api/notifications", "Inbox derived from your badge"),
            ("GET", "/api/search?q=honey", "Goods, stalls, shops"),
            ("GET", "/openapi.json", "Machine contract (JSON)"),
            ("GET", "/openapi.nish", "Machine contract (NISH)"),
        ]
        lines = "".join(
            f'<tr><td><span class="pill">{m}</span></td><td><code>{esc(p)}</code></td><td>{esc(d)}</td>'
            f'<td class="r"><a href="{esc(p)}">JSON</a> · '
            f'<a href="{esc(p)}{"&" if "?" in p else "?"}format=nish">NISH</a></td></tr>'
            for m, p, d in rows
        )
        body = f"""<p class="lede">Every row answers twice. Take the NISH link with the
Viewer extension and it paints a structured tree.</p>
<div class="panel"><table>{lines}</table></div>
<div class="panel"><h2>Posting NISH</h2>
<p>The till reads NISH too. <code>Content-Type: application/x-nish</code>:</p>
<pre>NISH/1.0

coupon = "WELCOME10"
address = "14 Mill Lane"</pre></div>"""
        return layout("API explorer", body, "explorer", req=req, email=me["email"])


# ---------------------------------------------------------------- JSON api


def _register_api(api: Blueprint) -> None:
    @api.get("/products")
    async def api_products(req, uid=Depends(current_user)):
        """Catalog: ?q=&cat=&sort=new|popular|rating|price_asc|price_desc&page=."""
        db = req.app.state_db
        q = req.query.get("q", "").strip()[:80]
        cat = req.query.get("cat", "")
        sort = req.query.get("sort", "new")
        page = max(1, int(req.query.get("page", "1") or 1))
        where, args = ["p.status = 'live'"], []
        if q:
            where.append("(p.name LIKE ? OR p.detail LIKE ?)")
            args += [f"%{q}%", f"%{q}%"]
        if cat in CATS:
            where.append("p.category = ?")
            args.append(cat)
        w = " AND ".join(where)
        order = {
            "new": "p.id DESC",
            "price_asc": "p.price_cents ASC",
            "price_desc": "p.price_cents DESC",
            "rating": "avg_stars DESC",
            "popular": "sold DESC",
        }.get(sort, "p.id DESC")
        total = (await db.fetch_one(f"SELECT COUNT(*) AS n FROM products p WHERE {w}", *args))["n"]
        rows = await db.fetch_all(
            "SELECT p.id, p.name, p.price_cents, p.stock, p.category, p.shop_id,"
            " COALESCE((SELECT AVG(stars) FROM reviews r WHERE r.product_id = p.id),0) AS avg_stars,"
            " COALESCE((SELECT SUM(qty) FROM order_items o WHERE o.product_id = p.id),0) AS sold"
            f" FROM products p WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
            *args,
            PER_PAGE,
            (page - 1) * PER_PAGE,
        )
        return negotiate(req, {"total": total, "page": page, "goods": [dict(r) for r in rows]})

    @api.get("/products/{pid:int}")
    async def api_product(req, pid: int, uid=Depends(current_user)):
        """One goods with rating, stock, and latest verdicts."""
        db = req.app.state_db
        p = await db.fetch_one("SELECT * FROM products WHERE id = ?", pid)
        if not p or p["status"] != "live":
            raise NotFound(f"no goods #{pid} on sale")
        agg = await db.fetch_one(
            "SELECT AVG(stars) AS a, COUNT(*) AS n FROM reviews WHERE product_id = ?", pid
        )
        revs = await db.fetch_all(
            "SELECT name, stars, body FROM reviews WHERE product_id = ? ORDER BY id DESC LIMIT 5", pid
        )
        d = dict(p)
        d.pop("image", None)
        d["has_image"] = bool(p["image"])
        return negotiate(
            req,
            {
                "goods": d,
                "avg_stars": agg["a"] or 0.0,
                "verdicts": agg["n"],
                "reviews": [dict(r) for r in revs],
            },
        )

    @api.get("/cart")
    async def api_cart(req, uid=Depends(current_user)):
        """Your basket with line maths."""
        lines = await req.app.state_db.fetch_all(
            "SELECT c.qty, p.id, p.name, p.price_cents, p.stock FROM cart c"
            " JOIN products p ON p.id = c.product_id WHERE c.user_id = ?",
            uid,
        )
        items = [dict(ln) for ln in lines]
        return negotiate(
            req,
            {
                "lines": len(items),
                "subtotal_cents": sum(i["price_cents"] * i["qty"] for i in items),
                "basket": items,
            },
        )

    @api.post("/cart")
    async def api_cart_add(req, uid=Depends(current_user)):
        """Drop goods in the basket: {"pid": 1, "qty": 2}."""
        try:
            body = await req.json()
        except Exception:
            raise BadRequest('send a JSON object — e.g. {"pid": 1, "qty": 2}')
        try:
            pid, qty = int(body.get("pid", 0)), max(1, int(body.get("qty", 1)))
        except (ValueError, TypeError):
            raise BadRequest("pid and qty must be whole numbers")
        p = await req.app.state_db.fetch_one("SELECT * FROM products WHERE id = ?", pid)
        if not p or p["status"] != "live":
            raise NotFound(f"no goods #{pid} on sale")
        if p["stock"] < 1:
            raise CheckoutFailed(f"“{p['name']}” is sold out")
        qty = min(qty, p["stock"])
        hit = await req.app.state_db.fetch_one(
            "SELECT qty FROM cart WHERE user_id = ? AND product_id = ?", uid, pid
        )
        if hit:
            await req.app.state_db.execute(
                "UPDATE cart SET qty = ?, added = ? WHERE user_id = ? AND product_id = ?",
                min(hit["qty"] + qty, p["stock"]),
                _now(),
                uid,
                pid,
            )
        else:
            await req.app.state_db.execute(
                "INSERT INTO cart (user_id, product_id, qty, added) VALUES (?, ?, ?, ?)",
                uid,
                pid,
                qty,
                _now(),
            )
        return negotiate(req, {"pid": pid, "qty": qty}, 201)

    @api.post("/checkout")
    async def api_checkout(req, uid=Depends(current_user)):
        """Ring the till. JSON or NISH body; Idempotency-Key replays safely."""
        ctype = req.headers.get("content-type", "").lower()
        if "nish" in ctype:
            raw = await req.nish()
            if not isinstance(raw, dict):
                raise BadRequest('NISH till slips are maps — e.g. address = "14 Mill Lane"')
            coupon, address = str(raw.get("coupon", "")), str(raw.get("address", ""))
        else:
            try:
                body = await req.json()
            except Exception:
                raise BadRequest('send a JSON object or NISH map — e.g. {"address": "14 Mill Lane"}')
            coupon = str((body or {}).get("coupon", ""))
            address = str((body or {}).get("address", ""))
        try:
            order = await run_checkout(req.app.state_db, uid, coupon, address)
        except CheckoutFailed as e:
            raise e
        return negotiate(
            req, {"code": order["code"], "total_cents": order["total"], "lines": order["lines"]}, 201
        )

    @api.get("/orders")
    async def api_orders(req, uid=Depends(current_user)):
        """Parcels, scoped to your badge (buyers: theirs; sellers: their stalls; Warden: all)."""
        me = await user_row(req, uid)
        db = req.app.state_db
        if me["role"] == "admin":
            rows = await db.fetch_all(
                "SELECT code, total, status, created FROM orders ORDER BY id DESC LIMIT 60"
            )
        elif me["role"] == "seller":
            shop = await db.fetch_one("SELECT id FROM shops WHERE owner_id = ?", uid)
            rows = (
                await db.fetch_all(
                    "SELECT DISTINCT o.code, o.total, o.status, o.created FROM orders o"
                    " JOIN order_items i ON i.order_id = o.id WHERE i.shop_id = ? ORDER BY o.id DESC LIMIT 60",
                    shop["id"],
                )
                if shop
                else []
            )
        else:
            rows = await db.fetch_all(
                "SELECT code, total, status, created FROM orders WHERE buyer_id = ? ORDER BY id DESC LIMIT 60",
                uid,
            )
        return negotiate(req, {"total": len(rows), "orders": [dict(r) for r in rows]})

    @api.get("/metrics")
    async def api_metrics(req, uid=Depends(current_user)):
        """Turnover, pipeline, top goods, per-stall rollup. Viewer-ready."""
        cached = await CACHE.get("metrics")
        if not isinstance(cached, dict):
            db = req.app.state_db
            gmv = (
                await db.fetch_one(
                    "SELECT COALESCE(SUM(total),0) AS g FROM orders WHERE status != 'cancelled'"
                )
            )["g"]
            pipe = await db.fetch_all("SELECT status, COUNT(*) AS n FROM orders GROUP BY status")
            top = await db.fetch_all(
                "SELECT product_id AS id, name, SUM(qty) AS sold, SUM(price_cents * qty) AS revenue"
                " FROM order_items GROUP BY product_id, name ORDER BY revenue DESC LIMIT 10"
            )
            stalls = await db.fetch_all(
                "SELECT s.id, s.name,"
                " COALESCE((SELECT SUM(i.price_cents * i.qty) FROM order_items i"
                " JOIN orders o ON o.id = i.order_id WHERE i.shop_id = s.id AND o.status != 'cancelled'),0) AS revenue,"
                " (SELECT COUNT(*) FROM products p WHERE p.shop_id = s.id AND p.status = 'live') AS live_goods"
                " FROM shops s ORDER BY s.id"
            )
            cached = {
                "gmv_cents": gmv,
                "pipeline": [dict(r) for r in pipe],
                "top_goods": [dict(r) for r in top],
                "stalls": [dict(r) for r in stalls],
            }
            await CACHE.set("metrics", cached, ttl=30)
        return negotiate(req, cached)

    @api.get("/notifications")
    async def api_notifications(req, uid=Depends(current_user)):
        """Inbox derived from your badge."""
        me = await user_row(req, uid)
        db = req.app.state_db
        items: list[dict] = []
        if me["role"] in ("buyer", "admin"):
            buyer_clause, buyer_args = ("", ()) if me["role"] == "admin" else ("AND od.buyer_id = ?", (uid,))
            for r in await db.fetch_all(
                "SELECT DISTINCT o.product_id, p.name FROM order_items o JOIN orders od ON od.id = o.order_id"
                f" JOIN products p ON p.id = o.product_id WHERE od.status = 'delivered' {buyer_clause}"
                " AND NOT EXISTS (SELECT 1 FROM reviews v WHERE v.product_id = o.product_id AND v.user_id = od.buyer_id)"
                " LIMIT 10",
                *buyer_args,
            ):
                items.append(
                    {"kind": "review", "text": f"Verdict due: “{r['name']}”", "href": f"/p/{r['product_id']}"}
                )
            ship_clause, ship_args = ("", ()) if me["role"] == "admin" else ("AND buyer_id = ?", (uid,))
            for o in await db.fetch_all(
                f"SELECT code FROM orders WHERE status = 'shipped' {ship_clause} LIMIT 10",
                *ship_args,
            ):
                items.append(
                    {
                        "kind": "transit",
                        "text": f"Parcel {o['code']} is on the road",
                        "href": f"/orders/{o['code']}",
                    }
                )
        if me["role"] in ("seller", "admin"):
            for p in await db.fetch_all(
                "SELECT p.id, p.name FROM products p"
                + ("" if me["role"] == "admin" else " JOIN shops s ON s.id = p.shop_id WHERE s.owner_id = ?")
                + (" WHERE " if me["role"] == "admin" else " AND ")
                + "p.stock = 0 AND p.status = 'live' LIMIT 10",
                *((uid,) if me["role"] != "admin" else ()),
            ):
                items.append(
                    {
                        "kind": "stock",
                        "text": f"“{p['name']}” is bare — restock",
                        "href": f"/sell/products/{p['id']}/edit",
                    }
                )
            for p in await db.fetch_all(
                "SELECT p.id, p.name FROM products p"
                + ("" if me["role"] == "admin" else " JOIN shops s ON s.id = p.shop_id WHERE s.owner_id = ?")
                + (" WHERE " if me["role"] == "admin" else " AND ")
                + "p.status = 'rejected' LIMIT 10",
                *((uid,) if me["role"] != "admin" else ()),
            ):
                items.append(
                    {
                        "kind": "moderation",
                        "text": f"“{p['name']}” sent back by the Warden",
                        "href": f"/sell/products/{p['id']}/edit",
                    }
                )
        if me["role"] == "admin":
            n = (await db.fetch_one("SELECT COUNT(*) AS n FROM products WHERE status = 'pending'"))["n"]
            if n:
                items.append({"kind": "moderation", "text": f"{n} goods awaiting chalk", "href": "/admin"})
        return negotiate(req, {"today": _today(), "badge": me["role"], "total": len(items), "items": items})

    @api.get("/search")
    async def api_search(req, uid=Depends(current_user)):
        """Goods, stalls, shops in one sweep: ?q=…."""
        q = req.query.get("q", "").strip()[:80]
        if not q:
            raise BadRequest("pass ?q=… — e.g. /api/search?q=honey")
        like = f"%{q}%"
        db = req.app.state_db
        return negotiate(
            req,
            {
                "q": q,
                "goods": [
                    dict(r)
                    for r in await db.fetch_all(
                        "SELECT id, name, price_cents FROM products WHERE status = 'live' AND name LIKE ? LIMIT 10",
                        like,
                    )
                ],
                "stalls": [
                    dict(r)
                    for r in await db.fetch_all("SELECT id, name FROM shops WHERE name LIKE ? LIMIT 10", like)
                ],
            },
        )


# ---------------------------------------------------------------- websocket


def _register_ws(app: Ikarem) -> None:
    @app.websocket("/ws/ticker")
    async def ticker(ws: WebSocket):
        await ws.accept()
        await TICKER.join(ws)
        try:
            while True:
                await ws.receive_text()  # heartbeat in; sales out
        except WebSocketDisconnect:
            pass
        finally:
            TICKER.leave(ws)


app = create_app()
