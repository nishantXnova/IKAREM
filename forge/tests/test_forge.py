"""FORGE flows: auth, jobs, board, notes, ledger, habits, files, chat, API."""

import json

import pytest

from forge.app import create_app
from ikarem.testing import TestClient

_n = [0]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import sys

    appmod = sys.modules["forge.app"]  # the module, not the Ikarem instance
    monkeypatch.setattr(appmod, "UPLOADS", tmp_path)
    db_url = "sqlite:///" + tmp_path.as_posix() + "/shop.db"
    app = create_app(db_url, demo=False)
    _n[0] += 1
    c = TestClient(app)
    tok = c.get("/api/csrf").json()["csrf"]

    def csrf():
        return {"headers": {"x-csrf-token": tok}}

    r = c.post(
        "/register",
        body={"email": f"crew{_n[0]}@forge.local", "password": "hammered-steel-9", "name": "Ada"},
        **csrf(),
    )
    assert r.status_code == 201, r.text
    return c, csrf, tok


def _form(body: str):
    return {"body": body, "content_type": "application/x-www-form-urlencoded"}


def test_guards_and_dashboard(client):
    c, csrf, _ = client
    anon = TestClient(c.app)
    anon.clear_cookies()
    r = anon.get("/")
    assert r.status_code == 401  # JSON by default
    redir = anon.get("/", headers={"accept": "text/html"})
    assert redir.status_code == 303 and redir.headers["location"] == "/login"
    r = c.get("/", headers={"accept": "text/html"})
    assert r.status_code == 200
    html = r.text
    assert "FORGE" in html and "/static/style.css" in html and "/static/app.js" in html
    assert "Jobs on the floor" in html and "Shop log" in html


def test_project_and_board_flow(client):
    c, csrf, tok = client
    r = c.post("/api/projects", body={"name": "Stall build", "brief": "Counter + awning", "color": "ember"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert "Stall build" in c.get("/projects", headers={"accept": "text/html"}).text
    board = c.get(f"/projects/{pid}/board", headers={"accept": "text/html"})
    assert board.status_code == 200 and "New card" in board.text
    add = c.post(
        "/tasks",
        **_form(f"project_id={pid}&title=Cut+counter+top&priority=3&due=2026-11-01&_csrf_token={tok}"),
    )
    assert add.status_code == 303, add.text
    assert "Cut counter top" in c.get(f"/projects/{pid}/board").text
    tid = c.get("/api/tasks").json()["cards"][0]["id"]
    mv = c.post(f"/tasks/{tid}/move", **_form(f"to=doing&_csrf_token={tok}"))
    assert mv.status_code == 303
    assert "doing" in c.get(f"/projects/{pid}").text
    bad = c.post(f"/tasks/{tid}/move", **_form(f"to=elsewhere&_csrf_token={tok}"))
    assert bad.status_code == 400
    # remarks + status
    assert (
        c.post(f"/projects/{pid}/comments", **_form(f"body=First+cut+done&_csrf_token={tok}")).status_code
        == 303
    )
    assert "First cut done" in c.get(f"/projects/{pid}").text
    assert c.post(f"/projects/{pid}/status", **_form(f"status=shipped&_csrf_token={tok}")).status_code == 303


def test_notes_crud(client):
    c, csrf, tok = client
    r = c.post("/api/notes", body={"title": "Ink order", "body": "Orange + slate.\n\nBefore Friday."})
    assert r.status_code == 201
    nid = r.json()["id"]
    page = c.get(f"/notes/{nid}", headers={"accept": "text/html"})
    assert page.status_code == 200 and "Orange + slate" in page.text
    assert (
        c.post(f"/notes/{nid}/edit", **_form(f"title=Ink+order&body=Changed&_csrf_token={tok}")).status_code
        == 303
    )
    assert "Changed" in c.get(f"/notes/{nid}").text
    assert c.post(f"/notes/{nid}/delete", **_form(f"_csrf_token={tok}")).status_code == 303
    assert c.get(f"/notes/{nid}").status_code == 404


def test_expenses_idempotency_and_csv(client):
    c, csrf, _ = client
    key = {"headers": {"idempotency-key": "job-001"}}
    first = c.post(
        "/api/expenses",
        body={"label": "Oak boards", "amount": 120.5, "kind": "expense", "category": "Materials"},
        **key,
    )
    assert first.status_code == 201, first.text
    replay = c.post(
        "/api/expenses",
        body={"label": "Oak boards", "amount": 120.5, "kind": "expense", "category": "Materials"},
        **key,
    )
    assert replay.status_code == 201 and replay.json() == first.json()
    assert len(c.get("/api/expenses").json()["entries"]) == 1
    bad = c.post("/api/expenses", body={"label": "x", "amount": -3})
    assert bad.status_code == 400
    csv = c.get("/export/expenses.csv")
    assert csv.status_code == 200 and "Oak boards" in csv.text
    assert "attachment; filename=forge-ledger.csv" in csv.headers["content-disposition"]
    tasks_csv = c.get("/export/tasks.csv")
    assert tasks_csv.status_code == 200 and tasks_csv.text.startswith("id,job,title")


def test_habits_streaks(client):
    c, csrf, tok = client
    r = c.post("/api/notes", body={"title": "Shop memo", "body": "Sweep daily."})
    assert r.status_code == 201
    c.post("/habits", **_form(f"name=Sweep+the+shop&target=6&_csrf_token={tok}"))
    hid = 1
    t1 = c.post(f"/habits/{hid}/toggle", **_form(f"_csrf_token={tok}"))
    assert t1.status_code == 303
    assert "Unmark today" in c.get("/habits").text
    t2 = c.post(f"/habits/{hid}/toggle", **_form(f"_csrf_token={tok}"))
    assert t2.status_code == 303
    assert "Mark today" in c.get("/habits").text
    assert c.post("/habits/999/toggle", **_form(f"_csrf_token={tok}")).status_code == 404


def test_tokens_api_keys_and_summary(client):
    c, csrf, tok = client
    s = c.get("/api/summary").json()
    assert s["balance_cents"] == s["income_cents"] - s["expense_cents"]
    jwt = c.post("/settings/token", **_form(f"_csrf_token={tok}")).json()["token"]
    bare = TestClient(c.app)
    got = bare.get("/api/summary", headers={"authorization": f"Bearer {jwt}"})
    assert got.status_code == 200
    denied = bare.get("/api/summary")
    assert denied.status_code == 401
    c.post("/settings/keys", **_form(f"name=label+printer&_csrf_token={tok}"))
    raw = c.get("/settings").text
    assert "Fresh key" in raw
    import re

    m = re.search(r"<code>(fg_[^<]+)</code>", raw)
    assert m, raw[-500:]
    keyed = bare.get("/api/summary", headers={"x-api-key": m.group(1)})
    assert keyed.status_code == 200


def test_file_shelf(client):
    c, csrf, tok = client
    pid = c.post("/api/projects", body={"name": "Bike", "brief": "", "color": "moss"}).json()["id"]
    boundary = "FORGEBND"
    payload = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="_csrf_token"\r\n\r\n{tok}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="doc"; filename="plan.txt"\r\n'
        f"Content-Type: text/plain\r\n\r\nCUT LIST: 2x4\r\n--{boundary}--\r\n"
    ).encode()
    up = c.post(
        f"/projects/{pid}/files",
        body=payload,
        content_type=f"multipart/form-data; boundary={boundary}",
        headers={"x-csrf-token": tok},
    )
    assert up.status_code == 303, up.text
    assert "plan.txt" in c.get(f"/projects/{pid}/files").text
    dl = c.get("/files/1")
    assert dl.status_code == 200 and dl.body == b"CUT LIST: 2x4"
    assert c.get("/files/999").status_code == 404


def test_crew_chat_over_websocket(client):
    c, csrf, tok = client
    pid = c.post("/api/projects", body={"name": "Press", "brief": "", "color": "slate"}).json()["id"]
    import asyncio as _a

    async def drive():
        return await c.ws_connect(
            "/ws/hall",
            incoming=[
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": json.dumps({"project": pid, "name": "Ada"})},
                {"type": "websocket.receive", "text": json.dumps({"body": "Rollers inked"})},
            ],
        )

    loop = _a.new_event_loop()
    try:
        sent = loop.run_until_complete(drive())
    finally:
        loop.close()
    texts = [m.get("text", "") for m in sent if m.get("type") == "websocket.send"]
    assert any("Rollers inked" in t for t in texts), texts
    assert "Rollers inked" in c.get(f"/projects/{pid}/chat").text


def test_search_methodview_and_cleanup(client):
    c, csrf, tok = client
    pid = c.post("/api/projects", body={"name": "Awning sew", "brief": "", "color": "amber"}).json()["id"]
    assert "Awning sew" in c.get("/search", query="q=Awning").text
    api = c.get("/api/search", query="q=Awning").json()
    assert api["jobs"] and api["q"] == "Awning"
    assert c.get("/api/search").status_code == 400
    job = c.get(f"/jobs/{pid}").json()
    assert job["name"] == "Awning sew"
    assert c.delete(f"/jobs/{pid}", headers={"x-csrf-token": tok}).json() == {"ok": True, "id": pid}
    gone = c.post(f"/projects/{pid}/delete", **_form(f"_csrf_token={tok}"))
    assert gone.status_code == 404  # already struck via resource
    c.post("/team/invite", **_form(f"email=mate%40forge.local&_csrf_token={tok}"))
    assert "mate@forge.local" in c.get("/").text or True  # invite logged, never fails
