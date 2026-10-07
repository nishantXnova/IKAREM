"""Relay flows: auth, services, incidents, feed, ingest, probes, debug, MCP."""

import asyncio
import os

# DB_URL must be visible at relay import: Config snapshots env ONCE at app
# construction (later changes are invisible). DEMO is deliberately NOT set
# here — import-time env edits poison sibling suites through the same
# snapshot (env beats constructor kwargs). The seed flag is set on our own
# config object instead (see _seed_demo_config).
os.environ.setdefault("IKAREM_DB_URL", "sqlite:///:memory:")

import pytest

import relay.app as appmod
from ikarem.testing import TestClient
from relay.app import app, service_list, status_rollup


@pytest.fixture(scope="module", autouse=True)
def _seed_demo_config():
    """Declare our seed precondition on our own config object: the seed
    users (admin/responder logins) exist before the first request starts
    the app. Touches nothing global — sibling suites keep their env."""
    app.config["demo"] = True
    yield


@pytest.fixture(autouse=True)
def _clear_nitro():
    yield
    status_rollup.cache_clear()
    service_list.cache_clear()


_n = [0]


def _client(email=None, password="s3cretpw"):
    _n[0] += 1
    c = TestClient(app)
    tok = c.get("/api/csrf").json()["csrf"]
    headers = {"headers": {"x-csrf-token": tok}}
    email = email or f"rel{_n[0]}@ex.co"
    r = c.post("/register", body={"email": email, "password": password}, **headers)
    assert r.status_code == 201, r.text
    return c, headers, email


def _api_token(email, password):
    c = TestClient(app)
    r = c.post("/api/login", body={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _bearer(token):
    return {"headers": {"authorization": f"Bearer {token}"}}


def _mint_raw_key():
    """Mint through the real settings flow; parse the once-shown secret."""
    import re

    c, headers, _ = _client()
    assert c.post("/settings/keys", body={"name": "t"}, **headers).status_code == 303
    m = re.search(r"rl_[A-Za-z0-9_\-]+", c.get("/settings").text)
    assert m, "minted key never flashed"
    return m.group(0)


def test_dashboard_renders_brand_and_cards():
    c, _, _ = _client()
    r = c.get("/")
    assert r.status_code == 200
    assert "RELAY" in r.text and "/static/style.css" in r.text
    assert "open</span>" in r.text


def test_register_login_logout_flow():
    c, headers, email = _client()
    assert "Log in" not in c.get("/").text  # logged in: no login prompt
    assert c.post("/logout", body={}, **headers).status_code == 303
    assert "Log in" in c.get("/").text
    # logout killed the session: mint a fresh CSRF token before logging back in
    headers = {"headers": {"x-csrf-token": c.get("/api/csrf").json()["csrf"]}}
    bad = c.post("/login", body={"email": email, "password": "wrongpw"}, **headers)
    assert bad.status_code == 401  # JSON client: JSON error, not a re-rendered form
    ok = c.post("/login", body={"email": email, "password": "s3cretpw"}, **headers)
    assert ok.status_code == 200 and ok.json()["ok"] is True


def test_register_validation_and_duplicates():
    c = TestClient(app)
    tok = c.get("/api/csrf").json()["csrf"]
    h = {"headers": {"x-csrf-token": tok}}
    assert "8-char" in c.post("/register", body={"email": "x@ex.co", "password": "short"}, **h).text
    assert "taken" in c.post("/register", body={"email": "admin@ex.co", "password": "s3cretpw"}, **h).text


def test_service_crud_html_and_validation():
    c, headers, _ = _client()
    r = c.post("/services", body={"name": "svc-a", "url": "https://a.ex.co/h"}, **headers)
    assert r.status_code == 303 and "/services/" in r.headers.get("location", "")
    assert "svc-a" in c.get("/services").text
    bad = c.post("/services", body={"name": "x", "url": "ftp://nope"}, **headers)
    assert bad.status_code == 200 and "http(s)" in bad.text
    assert c.get("/services/999999").status_code == 404
    anon = TestClient(app)
    # no session at all: the CSRF wall answers first (correct layering)
    assert anon.post("/services", body={"name": "x", "url": "https://x.co"}).status_code == 403
    # session (CSRF minted) but never logged in: the handler's own 401
    tok = anon.get("/api/csrf").json()["csrf"]
    r = anon.post("/services", body={"name": "x", "url": "https://x.co"}, headers={"x-csrf-token": tok})
    assert r.status_code == 401


def test_service_name_xss_escaped():
    c, headers, _ = _client()
    c.post("/services", body={"name": "<script>alert(1)</script>", "url": "https://x.co"}, **headers)
    html = c.get("/services").text
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_incident_open_ack_resolve_flow():
    c, headers, _ = _client()
    r = c.post("/incidents", body={"title": "db down", "service_id": ""}, **headers)
    assert r.status_code == 303
    iid = r.headers["location"].rsplit("/", 1)[-1]
    assert "db down" in c.get(f"/incidents/{iid}").text
    assert c.post(f"/incidents/{iid}/ack", body={}, **headers).status_code == 303
    assert "acked" in c.get(f"/incidents/{iid}").text
    assert c.post(f"/incidents/{iid}/resolve", body={}, **headers).status_code == 303
    assert "resolved" in c.get(f"/incidents/{iid}").text
    assert c.get("/incidents/999999").status_code == 404


def test_incident_broadcast_reaches_feed_members():
    seen = []

    class _Stub:
        async def send_json(self, msg):
            seen.append(msg)

        async def send_text(self, msg):
            seen.append(msg)

    stub = _Stub()
    asyncio.run(appmod.feed.join(stub))
    try:
        c, headers, _ = _client()
        c.post("/incidents", body={"title": "feed me", "service_id": ""}, **headers)
    finally:
        appmod.feed.leave(stub)
    assert any(m.get("event") == "incident.opened" and m.get("title") == "feed me" for m in seen)


def test_ws_feed_accepts_and_disconnects():
    async def go():
        c = TestClient(app)
        return await c.ws_connect("/ws/feed", [{"text": "ping"}])

    sent = asyncio.run(go())
    assert {"type": "websocket.accept"} in sent


def test_api_login_and_rbac():
    viewer_tok = _api_token("admin@ex.co", "admin1234")  # admin passes responder gates too
    op_tok = _api_token("op@ex.co", "op1234")
    c = TestClient(app)
    assert c.post("/api/login", body={"email": "admin@ex.co", "password": "nope"}).status_code == 401
    admin_headers = _bearer(viewer_tok)
    r = c.post("/api/services", body={"name": "api-svc", "url": "https://api.ex.co"}, **admin_headers)
    assert r.status_code == 201, r.text
    assert c.post("/api/services", body={"name": "x", "url": "https://x.co"}).status_code == 401
    assert (
        c.post(
            "/api/incidents",
            body={"title": "api incident", "service_id": 0},
            **_bearer(op_tok),
        ).status_code
        == 201
    )
    bad = c.post("/api/services", body={"name": "x", "url": "gopher://x"}, **admin_headers)
    assert bad.status_code == 400


def test_viewer_cannot_write_api():
    c, _, email = _client()
    tok = _api_token(email, "s3cretpw")
    assert (
        c.post("/api/services", body={"name": "x", "url": "https://x.co"}, **_bearer(tok)).status_code == 403
    )
    assert c.get("/api/services").status_code == 200
    assert c.get("/api/incidents").status_code == 200


def test_api_keys_mint_revoke_and_ingest():
    c, headers, _ = _client()
    r = c.post("/settings/keys", body={"name": "ing-1"}, **headers)
    assert r.status_code == 303
    settings = c.get("/settings").text
    assert "ing-1" in settings
    assert "shows once): rl_" in settings  # the secret flashes exactly once...
    assert "rl_" not in c.get("/settings").text  # ...and never again (hashed at rest)

    raw = _mint_raw_key()
    api = TestClient(app)
    key_h = {"headers": {"x-api-key": raw}}
    assert api.post("/api/ingest", body={"results": []}, **key_h).status_code == 201
    assert api.post("/api/ingest", body={"nope": 1}, **key_h).status_code == 400
    assert api.post("/api/ingest", body={"results": []}).status_code == 401
    assert api.post("/api/ingest", body={"results": []}, headers={"x-api-key": "rl_bogus"}).status_code == 401


def test_ingest_records_probes_and_opens_incidents():
    raw = _mint_raw_key()
    api = TestClient(app)
    admin = _bearer(_api_token("admin@ex.co", "admin1234"))
    sid = api.post("/api/services", body={"name": "ingest-svc", "url": "https://i.ex.co"}, **admin).json()[
        "id"
    ]
    key_h = {"headers": {"x-api-key": raw}}
    before = api.get("/api/status").json()["open_incidents"]
    r = api.post(
        "/api/ingest",
        body={"results": [{"service_id": sid, "ok": False, "ms": 12, "code": 500}]},
        **key_h,
    )
    assert r.status_code == 201 and r.json()["incidents_triggered"] == 1
    status_rollup.cache_clear()
    assert api.get("/api/status").json()["open_incidents"] == before + 1
    # duplicate failure: deduped (0 newly opened), count unchanged
    r2 = api.post(
        "/api/ingest", body={"results": [{"service_id": sid, "ok": False, "ms": 9, "code": 500}]}, **key_h
    )
    assert r2.json()["incidents_triggered"] == 0
    status_rollup.cache_clear()
    assert api.get("/api/status").json()["open_incidents"] == before + 1


def test_ingest_flood_stays_healthy_under_spike():
    import threading

    raw = _mint_raw_key()
    sid = TestClient(app).get("/api/services").json()["services"][0]["id"]
    codes = []

    def worker():
        conn_client = TestClient(app)
        r = conn_client.post(
            "/api/ingest",
            body={"results": [{"service_id": sid, "ok": True, "ms": 3, "code": 200}]},
            headers={"x-api-key": raw},
        )
        codes.append(r.status_code)

    ts = [threading.Thread(target=worker) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert codes and all(s == 201 for s in codes)


def test_status_rollup_is_nitro_cached():
    api = TestClient(app)
    first = api.get("/api/status").json()
    info = status_rollup.cache_info()
    assert info["misses"] >= 1
    api.get("/api/status")
    assert status_rollup.cache_info()["hits"] >= info["hits"] + 1
    assert api.get("/api/status").json() == first


def test_probe_all_records_and_auto_incidents(monkeypatch):
    import urllib.error

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout=5):
        if "down" in url:
            raise urllib.error.URLError("nope")
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    async def go():
        db = app.state_db
        await db.execute(
            "INSERT INTO services (name, url, owner_id, created) VALUES (?, ?, ?, ?)",
            "probe-ok",
            "https://ok.ex.co/",
            "t",
            1.0,
        )
        await db.execute(
            "INSERT INTO services (name, url, owner_id, created) VALUES (?, ?, ?, ?)",
            "probe-down",
            "https://down.ex.co/",
            "t",
            1.0,
        )
        await appmod.probe_all(app)
        ok_svc = await db.fetch_one("SELECT id FROM services WHERE name = 'probe-ok'")
        down_svc = await db.fetch_one("SELECT id FROM services WHERE name = 'probe-down'")
        ok_row = await db.fetch_one(
            "SELECT ok FROM probe_results WHERE service_id = ? ORDER BY id DESC LIMIT 1", ok_svc["id"]
        )
        down_row = await db.fetch_one(
            "SELECT ok FROM probe_results WHERE service_id = ? ORDER BY id DESC LIMIT 1",
            down_svc["id"],
        )
        inc = await db.fetch_one(
            "SELECT id FROM incidents WHERE service_id = ? AND status != 'resolved'",
            down_svc["id"],
        )
        notes = await db.fetch_all("SELECT kind FROM notifications WHERE kind = 'incident.opened'")
        return ok_row["ok"], down_row["ok"], inc is not None, len(notes) >= 0

    ok, down, has_inc, _ = asyncio.run(go())

    # drain the queued notification the failure enqueued
    async def drain():
        return await app.state_queue.run_one()

    assert (ok, down, has_inc) == (1, 0, True)
    assert asyncio.run(drain()) is True
    asyncio.run(drain())
    from relay.app import status_rollup as _roll

    _roll.cache_clear()


def test_manual_probe_button(monkeypatch):
    import urllib.error

    def fake_urlopen(url, timeout=5):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    c, headers, _ = _client()

    async def sid():
        return (await app.state_db.fetch_one("SELECT id FROM services ORDER BY id LIMIT 1"))["id"]

    service_id = asyncio.run(sid())
    r = c.post(f"/services/{service_id}/probe", body={}, **headers)
    assert r.status_code == 303
    assert "Probed: down" in c.get(f"/services/{service_id}").text


def test_debug_pulse_requires_login_and_reports():
    anon = TestClient(app)
    assert "log in" in anon.get("/debug/ikarem").text.lower()
    c, _, _ = _client()
    html = c.get("/debug/ikarem").text
    assert "SpikeManager" in html and "Nitro" in html
    assert "Routes:" in html and "MCP tools:" in html


def test_mcp_routes_as_tools():
    import asyncio as _aio

    tools = app.mcp_tools()
    names = {t["name"] for t in tools}
    assert "api_status" in names
    out = _aio.run(app.mcp_call("api_status", {}))
    assert out.get("isError") is False


def test_check_has_no_errors_and_acknowledges_public_writes():
    rep = app.check()
    assert rep["errors"] == []
    assert any("/register" in w and "no bearer/API-key guard" in w for w in rep["warnings"])
    assert len(rep["routes"]) >= 28
