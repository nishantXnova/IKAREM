"""BAZAAR × NISH: negotiated catalog, till, metrics, inbox, explorer."""

from market.tests.test_market import _form, square  # noqa: F401  (shared square)

__all__ = ["square"]


def _live_goods(seller, stok, warden, wtok, **kw):
    from market.tests.test_market import _open_stall, _stock_product

    try:
        _open_stall(seller, stok)
    except AssertionError:
        pass
    pid = _stock_product(seller, stok, **kw)
    seller.post(f"/sell/products/{pid}/submit", **_form(f"_csrf_token={stok}"))
    r = warden.post(f"/admin/products/{pid}/approve", **_form(f"_csrf_token={wtok}"))
    assert r.status_code == 303, r.text
    return pid


def test_nish_catalog_and_search(square):
    b, s, stok, w, wtok = square["buyer"], square["seller"], square["stok"], square["warden"], square["wtok"]
    _live_goods(s, stok, w, wtok, name="Rye loaf", price="4.50", stock="10")
    js = b.get("/api/products").json()
    assert js["total"] == 1 and js["goods"][0]["name"] == "Rye loaf"
    r = b.get("/api/products", query="q=rye&format=nish")
    assert r.text.startswith("NISH/1.0\n") and "[[goods]]" in r.text
    assert '"Rye loaf"' in r.text
    one = b.get("/api/products/1", query="format=nish")
    assert "[goods]" in one.text and "avg_stars" in one.text
    assert b.get("/api/products/999").status_code == 404
    missing = b.get("/api/products/999", query="format=nish")
    assert missing.status_code == 404 and missing.text.startswith("NISH/1.0\n")
    found = b.get("/api/search", query="q=Rye&format=nish").text
    assert found.startswith("NISH/1.0\n")


def test_nish_till_full_duplex(square):
    b, btok, s, stok, w, wtok = (
        square["buyer"],
        square["btok"],
        square["seller"],
        square["stok"],
        square["warden"],
        square["wtok"],
    )
    pid = _live_goods(s, stok, w, wtok, name="Plum jam", price="6.50", stock="8")
    b.post("/cart/add", **_form(f"pid={pid}&qty=2&_csrf_token={btok}"))
    body = 'NISH/1.0\n\ncoupon = ""\naddress = "14 Mill Lane"\n'
    r = b.post("/api/checkout", body=body, content_type="application/x-nish")
    assert r.status_code == 201, r.text
    assert r.json()["total_cents"] == 1300
    as_nish = b.get("/api/orders", query="format=nish")
    assert as_nish.text.startswith("NISH/1.0\n") and "[[orders]]" in as_nish.text
    bad = b.post("/api/checkout", body="address = = broken\n", content_type="application/x-nish")
    assert bad.status_code == 400


def test_metrics_etag_inbox_explorer(square):
    b, btok, s, stok, w, wtok = (
        square["buyer"],
        square["btok"],
        square["seller"],
        square["stok"],
        square["warden"],
        square["wtok"],
    )
    _live_goods(s, stok, w, wtok, name="Honey", price="9.00", stock="0")
    oats = _live_goods(s, stok, w, wtok, name="Oats", price="2.00", stock="5")
    b.post("/cart/add", **_form(f"pid={oats}&qty=1&_csrf_token={btok}"))
    b.post("/checkout", **_form(f"coupon=&address=Home&_csrf_token={btok}"))
    m = b.get("/api/metrics", query="format=nish")
    assert m.text.startswith("NISH/1.0\n")
    assert "[[top_goods]]" in m.text and "[[stalls]]" in m.text
    etag = m.headers.get("etag")
    assert etag
    assert b.get("/api/metrics", query="format=nish", headers={"if-none-match": etag}).status_code == 304
    inbox = s.get("/api/notifications").json()
    assert any(i["kind"] == "stock" and "Honey" in i["text"] for i in inbox["items"]), inbox
    page = b.get("/explorer", headers={"accept": "text/html"})
    assert page.status_code == 200 and "/api/metrics" in page.text and "format=nish" in page.text
    assert b.get("/openapi.nish").text.startswith("NISH/1.0\n")


def test_ticker_wire(square):
    import asyncio as _a

    b = square["buyer"]

    async def drive():
        return await b.ws_connect(
            "/ws/ticker",
            incoming=[{"type": "websocket.connect"}, {"type": "websocket.receive", "text": "beat"}],
        )

    loop = _a.new_event_loop()
    try:
        sent = loop.run_until_complete(drive())
    finally:
        loop.close()
    assert sent[0]["type"] == "websocket.accept"
    home = b.get("/", headers={"accept": "text/html"})
    assert home.status_code == 200 and "wirelist" in home.text
