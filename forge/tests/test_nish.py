"""FORGE × NISH: negotiated shapes, NISH request bodies, ETags, explorer."""

import pytest

from forge.app import create_app
from ikarem.testing import TestClient

_n = [0]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import sys

    appmod = sys.modules["forge.app"]
    monkeypatch.setattr(appmod, "UPLOADS", tmp_path)
    db_url = "sqlite:///" + tmp_path.as_posix() + "/nish.db"
    app = create_app(db_url, demo=False)
    _n[0] += 1
    c = TestClient(app)
    tok = c.get("/api/csrf").json()["csrf"]
    r = c.post(
        "/register",
        body={"email": f"nish{_n[0]}@forge.local", "password": "viewer-paints-trees", "name": "Nish"},
        headers={"x-csrf-token": tok},
    )
    assert r.status_code == 201, r.text
    pid = c.post(
        "/api/projects", body={"name": "Press run", "brief": "Ink + paper", "color": "slate"}
    ).json()["id"]
    return c, pid


def _nish(c, path, **kw):
    kw.setdefault("query", "format=nish")
    return c.get(path, **kw)


def test_json_default_untouched_and_nish_shape(client):
    c, pid = client
    js = c.get("/api/metrics").json()
    assert js["jobs"] == 1 and "projects" in js
    assert "application/json" in c.get("/api/metrics").headers["content-type"]
    r = _nish(c, "/api/metrics")
    assert r.text.startswith("NISH/1.0\n")
    assert "text/plain" in r.headers["content-type"]
    assert "[[projects]]" in r.text and "[[rhythm]]" in r.text
    assert "open_cards = 1" not in r.text  # no cards yet
    assert "jobs = 1" in r.text
    assert "etag" in r.headers


def test_dossier_issues_activity_negotiate(client):
    c, pid = client
    c.post("/api/tasks", body={"title": "Rollers inked", "priority": 3})
    dossier = _nish(c, f"/api/projects/{pid}")
    assert dossier.status_code == 200
    assert dossier.text.startswith("NISH/1.0\n")
    assert '"Press run"' in dossier.text
    assert "[cards]" in dossier.text and "[shelves]" in dossier.text
    issues = _nish(c, f"/api/projects/{pid}/issues")
    assert "total = 0" in issues.text  # card above is loose (no job)
    mine = c.post(f"/api/projects/{pid}/issues", body={"title": "Fold signatures", "priority": 2})
    assert mine.status_code == 201
    issues = _nish(c, f"/api/projects/{pid}/issues")
    assert "total = 1" in issues.text and "[[issues]]" in issues.text
    assert "Fold signatures" in issues.text
    filtered = c.get(f"/api/projects/{pid}/issues", query="state=done&format=nish")
    assert "total = 0" in filtered.text
    feed = _nish(c, f"/api/projects/{pid}/activity")
    assert "Fold signatures" in feed.text and "[[events]]" in feed.text
    assert _nish(c, "/api/projects/999").status_code == 404
    missing = _nish(c, "/api/projects/999/issues")
    assert missing.status_code == 404 and missing.text.startswith("NISH/1.0\n")


def test_nish_request_body_full_duplex(client):
    c, pid = client
    body = 'NISH/1.0\n\ntitle = "Cut pine"\npriority = 3\ndue = "2026-11-01"\n'
    r = c.post(f"/api/projects/{pid}/issues", body=body, content_type="application/x-nish")
    assert r.status_code == 201, r.text
    assert r.json()["title"] == "Cut pine"
    as_nish = _nish(c, f"/api/projects/{pid}/issues")
    assert "Cut pine" in as_nish.text and "2026-11-01" in as_nish.text
    bad = c.post(f"/api/projects/{pid}/issues", body="title = = broken\n", content_type="application/x-nish")
    assert bad.status_code == 400
    scalar = c.post(f"/api/projects/{pid}/issues", body="just a string\n", content_type="application/x-nish")
    assert scalar.status_code == 400


def test_etag_repoll_304_and_notifications(client):
    c, pid = client
    first = _nish(c, "/api/metrics")
    etag = first.headers.get("etag")
    assert etag
    repoll = c.get("/api/metrics", query="format=nish", headers={"if-none-match": etag})
    assert repoll.status_code == 304 and repoll.body == b""
    # JSON shape etags too
    j1 = c.get("/api/metrics")
    assert "etag" in j1.headers
    j2 = c.get("/api/metrics", headers={"if-none-match": j1.headers["etag"]})
    assert j2.status_code == 304
    notes = c.get("/api/notifications").json()
    assert notes["today"] and isinstance(notes["items"], list)
    nn = _nish(c, "/api/notifications")
    assert nn.text.startswith("NISH/1.0\n")


def test_overdue_surfaces_in_notifications(client):
    c, pid = client
    c.post(
        f"/api/projects/{pid}/issues",
        body={"title": "Ancient debt", "priority": 1, "due": "2020-01-01"},
    )
    notes = c.get("/api/notifications").json()
    kinds = [i["kind"] for i in notes["items"]]
    assert "overdue" in kinds
    assert any("Ancient debt" in i["text"] for i in notes["items"])


def test_explorer_and_openapi_nish(client):
    c, pid = client
    page = c.get("/explorer", headers={"accept": "text/html"})
    assert page.status_code == 200
    assert "/api/metrics" in page.text and "format=nish" in page.text
    assert "/openapi.nish" in page.text
    contract = c.get("/openapi.nish")
    assert contract.status_code == 200 and contract.text.startswith("NISH/1.0\n")
