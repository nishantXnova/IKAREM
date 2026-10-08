"""ikarem-oauth: refresh rotation, reuse detection, code flow, presets.

Hermetic: SQLite memory DB per test, fake provider transports (no network).
"""

import pytest
from ikarem_oauth import (
    OAuthPlugin,
    OAuthProvider,
    RefreshStore,
    github_provider,
    google_provider,
    sign_state,
    verify_state,
)

from ikarem import Ikarem
from ikarem.db import DatabasePlugin

SECRET = "oauth-test-secret-32-bytes-long!"


def _app(plugin):
    app = Ikarem(enable_docs=False)
    app.register(DatabasePlugin("sqlite:///:memory:"))
    app.register(plugin)
    return app


def test_requires_database_plugin():
    app = Ikarem(enable_docs=False)
    with pytest.raises(ValueError, match="database"):
        app.register(OAuthPlugin(auth_secret=SECRET))


def test_plugin_config_validated():
    with pytest.raises(ValueError, match="auth_secret"):
        OAuthPlugin(auth_secret="short")
    with pytest.raises(ValueError, match="on_user"):
        OAuthPlugin(auth_secret=SECRET, provider=github_provider("id", "s"))


def test_refresh_lifecycle_rotate_reuse_revoke():
    import asyncio

    async def go():
        from ikarem.db import create_connector

        db = create_connector("sqlite:///:memory:")
        await db.connect()
        store = RefreshStore(db)
        await store.ensure()
        pair = await store.issue("u1", SECRET)
        assert pair["access_token"] and pair["refresh_token"] and pair["expires_in"] == 900
        pair2 = await store.rotate(pair["refresh_token"], SECRET)
        assert pair2["refresh_token"] != pair["refresh_token"]
        # Replaying a rotated token is indistinguishable from theft: chain dies.
        with pytest.raises(ValueError, match="reuse detected"):
            await store.rotate(pair["refresh_token"], SECRET)
        with pytest.raises(ValueError, match="revoked"):
            await store.rotate(pair2["refresh_token"], SECRET)
        with pytest.raises(LookupError, match="log in again"):
            await store.rotate("nope", SECRET)
        assert await store.revoke("nope") is False

    asyncio.new_event_loop().run_until_complete(go())


def test_reuse_detection_kills_chain():
    import asyncio

    async def go():
        from ikarem.db import create_connector

        db = create_connector("sqlite:///:memory:")
        await db.connect()
        store = RefreshStore(db, refresh_ttl=9999)
        await store.ensure()
        p1 = await store.issue("victim", SECRET)
        p2 = await store.rotate(p1["refresh_token"], SECRET)  # attacker steals p1, victim rotates
        with pytest.raises(ValueError, match="reuse detected"):
            await store.rotate(p1["refresh_token"], SECRET)  # attacker replays stolen p1
        with pytest.raises(ValueError, match="revoked"):  # victim's fresh token dies too
            await store.rotate(p2["refresh_token"], SECRET)

    asyncio.new_event_loop().run_until_complete(go())


def test_token_routes():
    from ikarem.testing import TestClient

    app = _app(OAuthPlugin(auth_secret=SECRET))
    c = TestClient(app)
    r = c.post("/oauth/token", body={"grant_type": "refresh_token", "refresh_token": "x"})
    assert r.status_code == 401 and "log in again" in r.text  # unknown token, honest shape
    bad = c.post("/oauth/token", body={"grant_type": "password"})
    assert bad.status_code == 400 and "refresh_token" in bad.json()["detail"]
    assert c.post("/oauth/revoke", body={}).status_code == 400


def test_state_roundtrip_and_forgery():
    verify_state(sign_state(SECRET), SECRET)
    with pytest.raises(ValueError):
        verify_state(sign_state(SECRET)[:-2] + "xx", SECRET)  # tampered
    with pytest.raises(ValueError):
        verify_state(sign_state("other-secret"), SECRET)  # foreign secret
    with pytest.raises(ValueError):
        verify_state(sign_state(SECRET, ttl=-1), SECRET)  # stale
    with pytest.raises(ValueError):
        verify_state("garbage", SECRET)


def _fake_provider():
    def _post(url, data):
        assert "token" in url
        assert data["code"] == "good-code"
        return {"access_token": "prov-access"}

    def _get(url, token):
        assert token == "prov-access"
        return {"id": 42, "login": "octo"}

    return OAuthProvider(
        "fake",
        "https://prov/authorize",
        "https://prov/token",
        "https://prov/me",
        "cid",
        "csec",
        http_post=_post,
        http_get=_get,
    )


def test_full_code_flow_with_fake_provider():
    from ikarem.testing import TestClient

    async def on_user(provider, info):
        return f"{provider}:{info['id']}"

    app = _app(OAuthPlugin(auth_secret=SECRET, provider=_fake_provider(), on_user=on_user))
    c = TestClient(app)
    login = c.get("/oauth/login", query="redirect_uri=http://app/oauth/callback")
    assert login.status_code == 302
    loc = login.headers.get("location", "")
    assert "prov/authorize" in loc and "state=" in loc and "client_id=cid" in loc
    assert c.get("/oauth/login").status_code == 400  # missing redirect_uri names the fix
    state = sign_state(SECRET)
    done = c.get(
        "/oauth/callback", query=f"code=good-code&state={state}&redirect_uri=http://app/oauth/callback"
    )
    assert done.status_code == 200, done.text[:200]
    pair = done.json()
    assert pair["access_token"] and pair["refresh_token"]
    bad = c.get("/oauth/callback", query="code=good-code&state=forged&redirect_uri=http://app/oauth/callback")
    assert bad.status_code == 400
    denied = c.get("/oauth/callback", query="error=access_denied&state=x")
    assert denied.status_code == 400 and "access_denied" in denied.text


def test_presets_point_at_real_providers():
    gh = github_provider("id", "secret")
    assert "github.com/login/oauth" in gh.authorize_url and gh.userinfo_url.endswith("/user")
    go = google_provider("id", "secret")
    assert "accounts.google.com" in go.authorize_url and "googleapis" in go.token_url
    with pytest.raises(ValueError, match="client_id"):
        github_provider("", "")
