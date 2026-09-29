"""Cadence flows: streak engine, auth, habits, check-ins, export, API, guards."""

import os

os.environ.setdefault("IKAREM_DB_URL", "sqlite:///:memory:")

from datetime import date, timedelta

from cadence.app import app, current_streak, habit_stats, longest_streak
from ikarem.testing import TestClient

_n = [0]


def _client():
    _n[0] += 1
    c = TestClient(app)
    tok = c.get("/api/csrf").json()["csrf"]

    def csrf():
        return {"headers": {"x-csrf-token": tok}}

    email = f"user{_n[0]}@ex.co"
    r = c.post("/register", body={"email": email, "password": "s3cretpw"}, **csrf())
    assert r.status_code == 201, r.text
    # refresh token post-login (session rotated)
    tok = c.get("/api/csrf").json()["csrf"]

    def csrf2():
        return {"headers": {"x-csrf-token": tok}}

    return c, csrf2, email


def _make_habit(c, csrf, name="Read", **kw):
    body = {"name": name, "description": "", "color": "pine", "target_per_week": 7}
    body.update(kw)
    r = c.post("/api/habits", body=body, **csrf())
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --- streak engine (pure, no I/O) ---


def test_streaks_empty():
    assert current_streak(set(), date(2026, 3, 10)) == 0
    assert longest_streak(set()) == 0


def test_current_streak_counts_back_from_today():
    done = {"2026-03-08", "2026-03-09", "2026-03-10"}
    assert current_streak(done, date(2026, 3, 10)) == 3


def test_current_streak_survives_missing_today():
    done = {"2026-03-08", "2026-03-09"}
    assert current_streak(done, date(2026, 3, 10)) == 2


def test_current_streak_breaks_on_gap():
    done = {"2026-03-06", "2026-03-08", "2026-03-09", "2026-03-10"}
    assert current_streak(done, date(2026, 3, 10)) == 3


def test_longest_streak_finds_best_run():
    done = {"2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"}
    assert longest_streak(done) == 3


def test_habit_stats_week_and_rate():
    today = date(2026, 3, 10)  # a Tuesday
    done = {"2026-03-09", "2026-03-10", "2026-02-01"}
    st = habit_stats(done, today, 5)
    assert st == {
        "current": 2,
        "longest": 2,
        "week_done": 2,
        "week_target": 5,
        "last30_done": 2,
        "last30_rate": round(2 / 30, 3),
        "done_today": True,
    }


# --- full product flow ---


def test_register_login_habit_toggle_flow():
    c, csrf, _ = _client()
    assert "Nothing to track yet" in c.get("/").text

    hid = _make_habit(c, csrf, "Morning run", color="rust", target_per_week=5)

    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    r = c.post(f"/api/habits/{hid}/toggle", body={"day": yesterday}, **csrf())
    assert r.json()["done"] is True
    r = c.post(f"/api/habits/{hid}/toggle", body={}, **csrf())
    assert r.json() == {
        "id": hid,
        "day": today,
        "done": True,
        "current_streak": 2,
    }

    summary = c.get("/api/summary").json()
    assert summary["done_today"] == 1
    (h,) = summary["habits"]
    assert h["stats"]["current"] == 2
    assert h["stats"]["longest"] == 2
    assert h["color"] == "rust"

    dash = c.get("/").text
    assert "Morning run" in dash
    assert "1 of 1 done" in dash
    assert "2-day streak" in dash

    # untoggle today via the HTML form endpoint
    tok = c.get("/api/csrf").json()["csrf"]
    r = c.post(
        "/check",
        body=f"habit_id={hid}&day={today}&next=%2F&_csrf_token={tok}",
        content_type="application/x-www-form-urlencoded",
    )
    assert r.status_code == 303
    assert c.get("/api/summary").json()["done_today"] == 0


def test_toggle_rejects_bad_and_future_days():
    c, csrf, _ = _client()
    hid = _make_habit(c, csrf)
    assert c.post(f"/api/habits/{hid}/toggle", body={"day": "not-a-day"}, **csrf()).status_code == 400
    assert c.post(f"/api/habits/{hid}/toggle", body={"day": "2024-13-99"}, **csrf()).status_code == 400
    future = (date.today() + timedelta(days=1)).isoformat()
    r = c.post(f"/api/habits/{hid}/toggle", body={"day": future}, **csrf())
    assert r.status_code == 400


def test_habit_validation():
    c, csrf, _ = _client()
    bad_color = {"name": "x", "color": "neon", "target_per_week": 7}
    assert c.post("/api/habits", body=bad_color, **csrf()).status_code == 400
    bad_target = {"name": "x", "color": "pine", "target_per_week": 9}
    assert c.post("/api/habits", body=bad_target, **csrf()).status_code == 400
    empty_name = {"name": "", "color": "pine", "target_per_week": 7}
    assert c.post("/api/habits", body=empty_name, **csrf()).status_code == 400


def test_browser_form_flow():
    c = TestClient(app)
    tok = c.get("/api/csrf").json()["csrf"]
    c.post(
        "/register",
        body=f"email=form{_n[0]}@ex.co&password=s3cretpw&_csrf_token={tok}",
        content_type="application/x-www-form-urlencoded",
        headers={"accept": "text/html"},
    )
    _n[0] += 1
    tok = c.get("/api/csrf").json()["csrf"]
    r = c.post(
        "/habits",
        body=f"name=Floss&description=&color=teal&target_per_week=7&_csrf_token={tok}",
        content_type="application/x-www-form-urlencoded",
    )
    assert r.status_code == 303, r.text
    hid = c.get("/api/habits").json()[0]["id"]
    assert "Floss" in c.get("/").text
    assert f"/habits/{hid}" in c.get("/habits").text

    bad = c.post(
        "/habits",
        body=f"name=&color=teal&target_per_week=7&_csrf_token={c.get('/api/csrf').json()['csrf']}",
        content_type="application/x-www-form-urlencoded",
    )
    assert bad.status_code == 200
    assert "length must be" in bad.text


def test_export_csv():
    c, csrf, _ = _client()
    hid = _make_habit(c, csrf, "Meditate")
    c.post(f"/api/habits/{hid}/toggle", body={}, **csrf())
    r = c.get("/export.csv")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "Meditate" in r.text
    assert date.today().isoformat() in r.text


def test_users_cannot_see_each_others_habits():
    c1, csrf1, _ = _client()
    hid = _make_habit(c1, csrf1, "Private")
    c2, _, _ = _client()
    assert c2.get(f"/api/habits/{hid}").status_code == 404
    assert c2.get("/api/summary").json()["habits"] == []


def test_archive_restore_delete():
    c, csrf, _ = _client()
    hid = _make_habit(c, csrf, "Nap")
    tok = c.get("/api/csrf").json()["csrf"]
    form = {"content_type": "application/x-www-form-urlencoded"}

    c.post(f"/habits/{hid}/archive", body=f"_csrf_token={tok}", **form)
    assert c.get("/api/habits").json() == []
    assert "Archived" in c.get("/habits").text

    tok = c.get("/api/csrf").json()["csrf"]
    c.post(f"/habits/{hid}/archive", body=f"_csrf_token={tok}", **form)
    assert len(c.get("/api/habits").json()) == 1

    tok = c.get("/api/csrf").json()["csrf"]
    c.post(f"/habits/{hid}/delete", body=f"_csrf_token={tok}", **form)
    assert c.get(f"/api/habits/{hid}").status_code == 404


def test_history_endpoint():
    c, csrf, _ = _client()
    hid = _make_habit(c, csrf)
    c.post(f"/api/habits/{hid}/toggle", body={}, **csrf())
    r = c.get("/api/history", query="days=7").json()
    assert r["days"] == 7
    assert {"habit_id": hid, "day": date.today().isoformat()} in r["checkins"]


def test_static_assets_served():
    c = TestClient(app)
    css = c.get("/static/style.css")
    assert css.status_code == 200, css.status_code
    assert "--acc:" in css.text
    js = c.get("/static/app.js")
    assert js.status_code == 200
    assert "toggle-form" in js.text


def test_csrf_enforced_and_logout_ends_session():
    c, _, _ = _client()
    assert c.post("/api/habits", body={"name": "x"}).status_code in (401, 403)
    tok = c.get("/api/csrf").json()["csrf"]
    assert c.post("/logout", headers={"x-csrf-token": tok}).status_code == 200
    assert c.get("/api/habits").status_code == 401
