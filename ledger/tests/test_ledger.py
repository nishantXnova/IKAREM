"""Ledger flows: auth, dashboard, CRUD, upload, export, API, guards."""

import os

os.environ.setdefault("IKAREM_DB_URL", "sqlite:///:memory:")
os.environ.setdefault("IKAREM_DEMO", "false")

import pytest

from ikarem.testing import TestClient
from ledger.app import app


@pytest.fixture(autouse=True)
def _isolated_uploads(tmp_path, monkeypatch):
    import ledger.app as appmod

    monkeypatch.setattr(appmod, "UPLOADS", tmp_path)


_n = [0]


def _client():
    _n[0] += 1
    c = TestClient(app)
    tok = c.get("/api/csrf").json()["csrf"]

    def csrf():
        return {"headers": {"x-csrf-token": tok}}

    r = c.post("/register", body={"email": f"amy{_n[0]}@ex.co", "password": "s3cretpw"}, **csrf())
    assert r.status_code == 201, r.text
    return c, csrf


def test_dashboard_renders_charts():
    c, _ = _client()
    r = c.get("/")
    assert r.status_code == 200
    html = r.text
    assert "<svg" in html and "Last 6 months" in html and "Spending by category" in html
    assert "/static/style.css" in html


def test_html_crud_flow():
    c, csrf = _client()
    tok = csrf()["headers"]["x-csrf-token"]
    r = c.post(
        "/txns",
        body=f"description=Coffee&amount=3.50&kind=expense&category=Food&_csrf_token={tok}",
        content_type="application/x-www-form-urlencoded",
    )
    assert r.status_code == 303, r.text
    assert "Coffee" in c.get("/txns").text
    bad = c.post(
        "/txns",
        body=f"description=x&amount=-5&kind=expense&_csrf_token={tok}",
        content_type="application/x-www-form-urlencoded",
    )
    assert bad.status_code == 200 and "must be &gt; 0" in bad.text  # re-rendered with error


def test_api_crud_and_filters():
    c, csrf = _client()
    assert (
        c.post(
            "/api/txns",
            body={"description": "Paycheck", "amount": 2000, "kind": "income", "category": "Salary"},
            **csrf(),
        ).status_code
        == 201
    )
    assert (
        c.post(
            "/api/txns",
            body={"description": "Burger", "amount": 9, "kind": "expense", "category": "Food"},
            **csrf(),
        ).status_code
        == 201
    )
    assert c.post("/api/txns", body={"description": "bad", "amount": 0}, **csrf()).status_code == 400
    all_tx = c.get("/api/txns").json()
    assert all_tx["total"] == 2
    assert c.get("/api/txns", query="kind=income").json()["total"] == 1
    assert c.get("/api/txns", query="q=urg").json()["total"] == 1
    s = c.get("/api/summary").json()
    assert s["income_cents"] == 200000 and s["expense_cents"] == 900
    assert s["balance_cents"] == 199100
    nish = c.get("/api/summary", query="format=nish")
    assert nish.body.startswith(b"NISH/1.0")
    assert b"income_cents = 200000" in nish.body
    assert b"[[by_category]]" in nish.body
    tid = all_tx["txns"][0]["id"]
    assert c.put(f"/api/txns/{tid}", body={"description": "Paycheck!"}, **csrf()).status_code == 200
    assert c.delete(f"/api/txns/{tid}", **csrf()).json() == {"ok": True}
    assert c.get("/api/txns").json()["total"] == 1


def test_receipt_upload_and_download():
    c, csrf = _client()
    boundary = "BND"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="description"\r\n\r\nLunch\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="amount"\r\n\r\n12\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="receipt"; filename="r.txt"\r\n'
        f"Content-Type: text/plain\r\n\r\nRECEIPTDATA\r\n--{boundary}--\r\n"
    ).encode()
    tok = csrf()["headers"]["x-csrf-token"]
    r = c.post(
        "/txns",
        body=body,
        content_type=f"multipart/form-data; boundary={boundary}",
        headers={"x-csrf-token": tok},
    )
    assert r.status_code == 303, r.text
    tid = c.get("/api/txns").json()["txns"][0]["id"]
    dl = c.get(f"/receipts/{tid}")
    assert dl.status_code == 200 and dl.body == b"RECEIPTDATA"


def test_csv_export():
    c, csrf = _client()
    c.post("/api/txns", body={"description": "A", "amount": 1, "kind": "expense"}, **csrf())
    r = c.get("/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv"
    assert "date,description,amount_cents,kind,category" in r.text


def test_demo_seed_covers_all_categories():
    import asyncio

    from ledger.app import CATEGORIES, _seed_demo

    async def go():
        await app.startup()
        try:
            await _seed_demo(app.state_db)
        except Exception:
            pass  # demo-user may exist from another test; seed must still be total
        return await app.state_db.fetch_all("SELECT category FROM txns WHERE user_id = 'demo-user'")

    rows = asyncio.run(go())
    assert len(rows) >= 40, f"seed too small: {len(rows)}"
    # "Other" is only a fallback for user-created txns, never seeded
    assert set(CATEGORIES) - {"Other"} <= {r["category"] for r in rows}


def test_guards():
    anon = TestClient(app)
    assert anon.get("/").status_code == 401
    assert anon.get("/api/txns").status_code == 401
    assert anon.post("/api/txns", body={"description": "x", "amount": 1}).status_code in (401, 403)
    assert anon.post("/register", body={"email": "z@ex.co", "password": "s3cretpw"}).status_code == 403
    assert anon.get("/healthz").json()["status"] == "ok"
    assert "ikarem_requests" in anon.get("/metrics").text


def test_browser_anon_redirects_to_login():
    anon = TestClient(app)
    r = anon.get("/", headers={"accept": "text/html"})
    assert r.status_code == 303, r.text
    assert r.headers.get("location") == "/login"
    assert anon.get("/txns", headers={"accept": "text/html"}).status_code == 303


def test_form_login_failure_rerenders_with_error():
    c = TestClient(app)
    tok = c.get("/api/csrf").json()["csrf"]
    r = c.post(
        "/login",
        body=f"email=nobody@ex.co&password=wrong&_csrf_token={tok}",
        content_type="application/x-www-form-urlencoded",
    )
    assert r.status_code == 200, r.text
    assert "Invalid email or password." in r.text
    assert "nobody@ex.co" in r.text  # email preserved
    # JSON API still gets a clean 401 (with CSRF header, per secure default)
    assert (
        c.post(
            "/login", body={"email": "nobody@ex.co", "password": "wrong"}, headers={"x-csrf-token": tok}
        ).status_code
        == 401
    )


def test_form_register_duplicate_rerenders_with_error():
    import time

    email = f"dup{time.time_ns()}@ex.co"

    def reg(c, tok):
        return c.post(
            "/register",
            body=f"email={email}&password=s3cretpw&_csrf_token={tok}",
            content_type="application/x-www-form-urlencoded",
            headers={"accept": "text/html"},
        )

    c1 = TestClient(app)
    first = reg(c1, c1.get("/api/csrf").json()["csrf"])
    assert first.status_code == 303, first.text  # HTML form success redirects
    c2 = TestClient(app)
    dup = reg(c2, c2.get("/api/csrf").json()["csrf"])
    assert dup.status_code == 200
    assert "email already registered" in dup.text
    assert email in dup.text


def test_flash_shows_once_after_login():
    c = TestClient(app)
    tok = c.get("/api/csrf").json()["csrf"]
    # register via form to exercise the browser flash path
    import time

    email = f"flash{time.time_ns()}@ex.co"
    r = c.post(
        "/register",
        body=f"email={email}&password=s3cretpw&_csrf_token={tok}",
        content_type="application/x-www-form-urlencoded",
        headers={"accept": "text/html"},
    )
    assert r.status_code == 303
    dash = c.get("/", headers={"accept": "text/html"})
    assert "Account created" in dash.text
    assert "Account created" not in c.get("/", headers={"accept": "text/html"}).text


def test_mcp_transport_serves_api_tools():
    import json as _json

    c, csrf = _client()

    def rpc(payload):
        return c.post("/mcp", body=_json.dumps(payload).encode(), content_type="application/json", **csrf())

    init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init.status_code == 200
    assert init.json()["result"]["protocolVersion"] == "2024-11-05"
    names = {
        t["name"] for t in rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    }
    assert "api_summary" in names
    anon = TestClient(app)
    anon_tok = anon.get("/api/csrf").json()["csrf"]
    denied = anon.post(
        "/mcp",
        body=_json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "api_summary", "arguments": {}},
            }
        ).encode(),
        content_type="application/json",
        headers={"x-csrf-token": anon_tok},
    )
    assert denied.json()["result"]["isError"] is True  # no session: auth enforced over HTTP too
    jar = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
    tool_headers = {"x-csrf-token": c.get("/api/csrf").json()["csrf"], "cookie": jar}
    authed = c.post(
        "/mcp",
        body=_json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "api_list", "arguments": {"headers": tool_headers}},
            }
        ).encode(),
        content_type="application/json",
        **csrf(),
    )
    assert authed.json()["result"]["isError"] is False
