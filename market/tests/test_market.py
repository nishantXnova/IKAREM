"""BAZAAR flows: stalls, moderation, basket, atomic till, pipeline, wallet."""

import asyncio

import pytest

from ikarem.testing import TestClient
from market.app import create_app

_n = [0]


@pytest.fixture()
def square(tmp_path, monkeypatch):
    import sys

    appmod = sys.modules["market.app"]
    monkeypatch.setattr(appmod, "UPLOADS", tmp_path)
    db_url = "sqlite:///" + tmp_path.as_posix() + "/square.db"
    app = create_app(db_url, demo=False)
    _n[0] += 1
    tag = _n[0]
    loop = asyncio.new_event_loop()

    def sql(q, *a):
        return loop.run_until_complete(_exec(app, q, *a))

    async def _exec(app, q, *a):
        await app.startup()
        return await app.state_db.execute(q, *a)

    async def _one(app, q, *a):
        await app.startup()
        return await app.state_db.fetch_one(q, *a)

    def fetch(q, *a):
        return loop.run_until_complete(_one(app, q, *a))

    def user(email, pw="password-123", name="Trader"):
        c = TestClient(app)
        tok = c.get("/api/csrf").json()["csrf"]
        r = c.post(
            "/register",
            body={"email": email, "password": pw, "name": name},
            headers={"x-csrf-token": tok},
        )
        assert r.status_code == 201, r.text
        return c, tok

    try:
        buyer, btok = user(f"buyer{tag}@sq.local", name="June")
        seller, stok = user(f"seller{tag}@sq.local", name="Marta")
        warden, wtok = user(f"warden{tag}@sq.local", name="Warden")
        sql("UPDATE users SET role = 'seller' WHERE email = ?", f"seller{tag}@sq.local")
        sql("UPDATE users SET role = 'admin' WHERE email = ?", f"warden{tag}@sq.local")
        yield {
            "buyer": buyer,
            "btok": btok,
            "seller": seller,
            "stok": stok,
            "warden": warden,
            "wtok": wtok,
            "sql": sql,
            "fetch": fetch,
            "tag": tag,
        }
    finally:
        loop.close()


def _form(body: str):
    return {"body": body, "content_type": "application/x-www-form-urlencoded"}


def _open_stall(seller, stok, name="Marta's Pantry"):
    r = seller.post("/sell/shop", **_form(f"name={name}&blurb=Fine+goods&_csrf_token={stok}"))
    assert r.status_code == 303, r.text


def _stock_product(seller, stok, name="Rye loaf", price="4.50", stock="10", cat="Bakery"):
    boundary = "STALBOUND"
    payload = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="_csrf_token"\r\n\r\n{stok}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="name"\r\n\r\n{name}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="detail"\r\n\r\nBaked dawn.\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="price"\r\n\r\n{price}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="stock"\r\n\r\n{stock}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="category"\r\n\r\n{cat}\r\n'
        f"--{boundary}--\r\n"
    ).encode()
    r = seller.post(
        "/sell/products",
        body=payload,
        content_type=f"multipart/form-data; boundary={boundary}",
        headers={"x-csrf-token": stok},
    )
    assert r.status_code == 303, r.text
    return r.headers["location"].split("/")[-2]


def test_home_guards_and_grid(square):
    buyer, btok = square["buyer"], square["btok"]
    anon = TestClient(buyer.app)
    assert anon.get("/").status_code == 401
    assert anon.get("/", headers={"accept": "text/html"}).status_code == 303
    assert buyer.post("/sell/shop", **_form(f"name=June+Goods&_csrf_token={btok}")).status_code == 303
    pid = _stock_product(buyer, btok, name="Seed cake")
    buyer.post(f"/sell/products/{pid}/submit", **_form(f"_csrf_token={btok}"))
    home = buyer.get("/", headers={"accept": "text/html"})
    assert home.status_code == 200 and "BAZAAR WIRE" in home.text
    assert 'class="prod"' not in home.text  # pending ≠ live: grid stays empty
    assert buyer.get("/admin").status_code == 403


def test_moderation_publishes(square):
    s, stok, w, wtok, b = square["seller"], square["stok"], square["warden"], square["wtok"], square["buyer"]
    _open_stall(s, stok)
    pid = _stock_product(s, stok)
    s.post(f"/sell/products/{pid}/submit", **_form(f"_csrf_token={stok}"))
    assert "Rye loaf" in w.get("/admin").text
    assert w.post(f"/admin/products/{pid}/approve", **_form(f"_csrf_token={wtok}")).status_code == 303
    assert "Rye loaf" in b.get("/").text
    assert "Rye loaf" in b.get(f"/p/{pid}").text


def test_cart_checkout_atomic(square):
    s, stok, w, wtok, b, btok = (
        square["seller"],
        square["stok"],
        square["warden"],
        square["wtok"],
        square["buyer"],
        square["btok"],
    )
    _open_stall(s, stok)
    pid = _stock_product(s, stok, price="4.50", stock="10")
    s.post(f"/sell/products/{pid}/submit", **_form(f"_csrf_token={stok}"))
    w.post(f"/admin/products/{pid}/approve", **_form(f"_csrf_token={wtok}"))
    assert b.post("/cart/add", **_form(f"pid={pid}&qty=2&_csrf_token={btok}")).status_code == 303
    assert "$9.00" in b.get("/cart").text
    before = b.get("/api/products").json()
    assert before["total"] == 1
    r = b.post("/checkout", **_form(f"coupon=&address=14+Mill+Lane&_csrf_token={btok}"))
    assert r.status_code == 303, r.text
    code = r.headers["location"].split("/")[-1]
    assert code.startswith("BZ-")
    page = b.get(f"/orders/{code}")
    assert "paid" in page.text and "$9.00" in page.text
    assert "Placed" in page.text or "placed" in page.text
    assert "Basket is empty" in b.get("/cart").text
    prod = b.get(f"/p/{pid}").text
    assert "8 on the shelf" in prod  # 10 − 2
    wallet = b.get("/wallet").text
    assert "$191.00" in wallet  # 200 − 9


def test_double_ring_charges_once(square):
    s, stok, w, wtok, b, btok = (
        square["seller"],
        square["stok"],
        square["warden"],
        square["wtok"],
        square["buyer"],
        square["btok"],
    )
    _open_stall(s, stok)
    pid = _stock_product(s, stok, price="5.00", stock="10")
    s.post(f"/sell/products/{pid}/submit", **_form(f"_csrf_token={stok}"))
    w.post(f"/admin/products/{pid}/approve", **_form(f"_csrf_token={wtok}"))
    b.post("/cart/add", **_form(f"pid={pid}&qty=1&_csrf_token={btok}"))
    key = {"headers": {"idempotency-key": "till-001"}}
    first = b.post("/api/checkout", body={"address": "14 Mill Lane"}, **key)
    assert first.status_code == 201, first.text
    again = b.post("/api/checkout", body={"address": "14 Mill Lane"}, **key)
    assert again.status_code == 201 and again.json() == first.json()
    assert len(b.get("/api/orders").json()["orders"]) == 1
    assert "$195.00" in b.get("/wallet").text  # charged exactly once


def test_oversell_and_thin_wallet(square):
    s, stok, w, wtok, b, btok = (
        square["seller"],
        square["stok"],
        square["warden"],
        square["wtok"],
        square["buyer"],
        square["btok"],
    )
    _open_stall(s, stok)
    pid = _stock_product(s, stok, price="500.00", stock="1")
    s.post(f"/sell/products/{pid}/submit", **_form(f"_csrf_token={stok}"))
    w.post(f"/admin/products/{pid}/approve", **_form(f"_csrf_token={wtok}"))
    b.post("/cart/add", **_form(f"pid={pid}&qty=1&_csrf_token={btok}"))
    broke = b.post("/checkout", **_form(f"coupon=&address=Nowhere&_csrf_token={btok}"))
    assert broke.status_code == 400 and "top up" in broke.text
    b.post("/wallet/topup", **_form(f"amount=400.00&_csrf_token={btok}"))
    ok = b.post("/checkout", **_form(f"coupon=&address=Nowhere&_csrf_token={btok}"))
    assert ok.status_code == 303
    assert "Sold out" in b.get(f"/p/{pid}").text
    b.post("/cart/add", **_form(f"pid={pid}&qty=1&_csrf_token={btok}"))
    gone = b.post("/checkout", **_form(f"coupon=&address=Nowhere&_csrf_token={btok}"))
    assert gone.status_code == 400


def test_reviews_gated_and_unique(square):
    s, stok, w, wtok, b, btok = (
        square["seller"],
        square["stok"],
        square["warden"],
        square["wtok"],
        square["buyer"],
        square["btok"],
    )
    _open_stall(s, stok)
    pid = _stock_product(s, stok, price="2.00", stock="5")
    s.post(f"/sell/products/{pid}/submit", **_form(f"_csrf_token={stok}"))
    w.post(f"/admin/products/{pid}/approve", **_form(f"_csrf_token={wtok}"))
    no_buy = b.post(f"/p/{pid}/review", **_form(f"stars=5&body=Great&_csrf_token={btok}"))
    assert no_buy.status_code == 403
    b.post("/cart/add", **_form(f"pid={pid}&qty=1&_csrf_token={btok}"))
    b.post("/checkout", **_form(f"coupon=&address=Home&_csrf_token={btok}"))
    yes = b.post(f"/p/{pid}/review", **_form(f"stars=5&body=Great+crumb&_csrf_token={btok}"))
    assert yes.status_code == 303
    assert "Great crumb" in b.get(f"/p/{pid}").text
    dup = b.post(f"/p/{pid}/review", **_form(f"stars=1&body=Changed+mind&_csrf_token={btok}"))
    assert dup.status_code == 400


def test_cancel_refunds_and_restocks(square):
    s, stok, w, wtok, b, btok = (
        square["seller"],
        square["stok"],
        square["warden"],
        square["wtok"],
        square["buyer"],
        square["btok"],
    )
    _open_stall(s, stok)
    pid = _stock_product(s, stok, price="10.00", stock="4")
    s.post(f"/sell/products/{pid}/submit", **_form(f"_csrf_token={stok}"))
    w.post(f"/admin/products/{pid}/approve", **_form(f"_csrf_token={wtok}"))
    b.post("/cart/add", **_form(f"pid={pid}&qty=1&_csrf_token={btok}"))
    code = (
        b.post("/checkout", **_form(f"coupon=&address=Home&_csrf_token={btok}"))
        .headers["location"]
        .split("/")[-1]
    )
    assert b.post(f"/orders/{code}/cancel", **_form(f"_csrf_token={btok}")).status_code == 303
    assert "cancelled" in b.get(f"/orders/{code}").text
    assert "$200.00" in b.get("/wallet").text
    assert "4 on the shelf" in b.get(f"/p/{pid}").text
    stuck = b.post(f"/orders/{code}/advance", **_form(f"_csrf_token={btok}"))
    assert stuck.status_code in (400, 403)


def test_fulfillment_pipeline(square):
    s, stok, w, wtok, b, btok = (
        square["seller"],
        square["stok"],
        square["warden"],
        square["wtok"],
        square["buyer"],
        square["btok"],
    )
    _open_stall(s, stok)
    pid = _stock_product(s, stok, price="3.00", stock="6")
    s.post(f"/sell/products/{pid}/submit", **_form(f"_csrf_token={stok}"))
    w.post(f"/admin/products/{pid}/approve", **_form(f"_csrf_token={wtok}"))
    b.post("/cart/add", **_form(f"pid={pid}&qty=1&_csrf_token={btok}"))
    code = (
        b.post("/checkout", **_form(f"coupon=&address=Home&_csrf_token={btok}"))
        .headers["location"]
        .split("/")[-1]
    )
    assert b.post(f"/orders/{code}/advance", **_form(f"_csrf_token={btok}")).status_code == 403
    for want in ("packed", "shipped", "delivered"):
        assert s.post(f"/orders/{code}/advance", **_form(f"_csrf_token={stok}")).status_code == 303
        assert want in b.get(f"/orders/{code}").text
    assert s.post(f"/orders/{code}/advance", **_form(f"_csrf_token={stok}")).status_code == 400


def test_wishlist_wallet_and_admin(square):
    b, btok, w, wtok = square["buyer"], square["btok"], square["warden"], square["wtok"]
    s, stok = square["seller"], square["stok"]
    _open_stall(s, stok)
    pid = _stock_product(s, stok)
    s.post(f"/sell/products/{pid}/submit", **_form(f"_csrf_token={stok}"))
    w.post(f"/admin/products/{pid}/approve", **_form(f"_csrf_token={wtok}"))
    b.post("/wishlist", **_form(f"pid={pid}&_csrf_token={btok}"), headers={"referer": f"/p/{pid}"})
    assert "★ Saved" in b.get(f"/p/{pid}").text
    assert b.post("/wallet/topup", **_form(f"amount=25.00&_csrf_token={btok}")).status_code == 303
    assert "$225.00" in b.get("/wallet").text
    assert (
        w.post("/admin/coupons", **_form(f"code=SQUARE5&pct=5&max_uses=10&_csrf_token={wtok}")).status_code
        == 303
    )
    dup = w.post("/admin/coupons", **_form(f"code=SQUARE5&pct=5&max_uses=10&_csrf_token={wtok}"))
    assert dup.status_code == 400
    me = w.get("/api/orders").json()
    assert isinstance(me["orders"], list)
    export = w.get("/export/orders.csv")
    assert export.status_code == 200 and export.text.startswith("code,buyer,total_cents")
    assert b.get("/export/orders.csv").status_code == 403


def test_badges(square):
    w, wtok, b = square["warden"], square["wtok"], square["buyer"]
    fetch, tag = square["fetch"], square["tag"]
    assert "buyer" in w.get("/admin").text
    buyer_id = fetch("SELECT id FROM users WHERE email = ?", f"buyer{tag}@sq.local")["id"]
    warden_id = fetch("SELECT id FROM users WHERE email = ?", f"warden{tag}@sq.local")["id"]
    assert (
        w.post(f"/admin/users/{buyer_id}/role", **_form(f"role=seller&_csrf_token={wtok}")).status_code == 303
    )
    assert "Open a stall" in b.get("/sell").text  # badge took effect
    bad = w.post(f"/admin/users/{buyer_id}/role", **_form(f"role=duke&_csrf_token={wtok}"))
    assert bad.status_code == 400
    self_dem = w.post(f"/admin/users/{warden_id}/role", **_form(f"role=buyer&_csrf_token={wtok}"))
    assert self_dem.status_code == 400 and "unbade" in self_dem.text
    assert b.get("/admin").status_code == 403
