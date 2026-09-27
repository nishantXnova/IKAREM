"""Sessions + CSRF: login flow, tamper/expiry, double-submit."""

import time

from ikarem import CSRFMiddleware, Ikarem, SessionMiddleware, csrf_token
from ikarem.session import decode_session, encode_session
from ikarem.testing import TestClient

SECRET = "session-test-secret"


def _app():
    app = Ikarem(enable_docs=False, session_secret=SECRET)
    app.use(SessionMiddleware())
    app.use(CSRFMiddleware(exempt_paths=["/api/*"]))

    @app.post("/login")
    async def login(req):
        form = await req.form()
        if form.get("user") == "amy" and form.get("pw") == "s3cret":
            req.session["uid"] = "u1"
            return {"ok": True}
        from ikarem import Unauthorized

        raise Unauthorized("bad credentials")

    @app.get("/me")
    async def me(req):
        uid = req.session.get("uid")
        if not uid:
            from ikarem import Unauthorized

            raise Unauthorized("anonymous")
        return {"uid": uid}

    @app.post("/logout")
    async def logout(req):
        req.session.clear()
        return {"ok": True}

    @app.get("/form")
    async def form(req):
        return {"csrf": csrf_token(req)}

    @app.post("/note")
    async def note(req):
        return {"saved": (await req.form()).get("text")}

    @app.post("/api/hook")
    async def hook(req):
        return {"hooked": True}

    return app


def test_login_me_logout_cookie_flow():
    c = TestClient(_app())
    assert c.get("/me").status_code == 401
    r = c.post(
        "/login",
        body="user=amy&pw=s3cret",
        content_type="application/x-www-form-urlencoded",
        headers={"x-csrf-token": _token(c)},
    )
    assert r.status_code == 200, r.text
    assert "ikarem_session" in c.cookies  # jar persisted Set-Cookie
    assert c.get("/me").json() == {"uid": "u1"}
    assert c.post("/logout", headers={"x-csrf-token": _token(c)}).status_code == 200
    assert c.get("/me").status_code == 401


def _token(c):
    return c.get("/form").json()["csrf"]


def test_tampered_cookie_is_anonymous():
    c = TestClient(_app())
    c.cookies["ikarem_session"] = "forged.payload"
    assert c.get("/me").status_code == 401


def test_expired_session_is_anonymous():
    c = TestClient(_app())
    c.cookies["ikarem_session"] = encode_session({"uid": "u1"}, SECRET, max_age=-1)
    assert c.get("/me").status_code == 401


def test_cookie_flags():
    c = TestClient(_app())
    r = c.post(
        "/login",
        body="user=amy&pw=s3cret",
        content_type="application/x-www-form-urlencoded",
        headers={"x-csrf-token": _token(c)},
    )
    assert r.status_code == 200, r.text
    raw = "; ".join(v for k, v in r.headers_list if k.lower() == "set-cookie")
    assert "HttpOnly" in raw and "SameSite=Lax" in raw and "Max-Age=" in raw


def test_csrf_blocks_without_token():
    c = TestClient(_app())
    assert (
        c.post("/note", body="text=hi", content_type="application/x-www-form-urlencoded").status_code == 403
    )


def test_csrf_accepts_header_and_form_field():
    c = TestClient(_app())
    tok = _token(c)
    assert c.post(
        "/note",
        body="text=hi",
        content_type="application/x-www-form-urlencoded",
        headers={"x-csrf-token": tok},
    ).json() == {"saved": "hi"}
    c2 = TestClient(_app())
    tok2 = _token(c2)
    assert c2.post(
        "/note", body=f"text=yo&_csrf_token={tok2}", content_type="application/x-www-form-urlencoded"
    ).json() == {"saved": "yo"}


def test_csrf_exempt_api_path():
    c = TestClient(_app())
    assert c.post("/api/hook", body={}).status_code == 200


def test_reused_headers_dict_reads_fresh_cookies():
    """A shared headers dict must not freeze the pre-login session cookie."""
    c = TestClient(_app())
    h = {"headers": {"x-csrf-token": _token(c)}}
    c.post("/login", body="user=amy&pw=s3cret", content_type="application/x-www-form-urlencoded", **h)
    assert c.get("/me", **h).json() == {"uid": "u1"}


def test_session_roundtrip_helpers():
    tok = encode_session({"a": 1}, SECRET, 60)
    assert decode_session(tok, SECRET) == {"a": 1}
    assert decode_session(tok, "wrong-secret") is None
    assert decode_session("garbage", SECRET) is None
    time.sleep(0)
