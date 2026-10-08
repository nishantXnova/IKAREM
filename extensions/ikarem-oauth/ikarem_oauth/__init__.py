"""ikarem-oauth: refresh tokens + OAuth2 login for IKAREM apps."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from .flow import OAuthProvider, github_provider, google_provider, pkce_pair, sign_state, verify_state
from .tokens import RefreshStore

__all__ = [
    "OAuthPlugin",
    "OAuthProvider",
    "RefreshStore",
    "github_provider",
    "google_provider",
    "pkce_pair",
    "sign_state",
    "verify_state",
]

_PKCE_TABLE = "ikarem_oauth_pkce"
_NO_STORE = {"cache-control": "no-store", "pragma": "no-cache"}


def _is_local(url: str) -> bool:
    import urllib.parse

    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:  # noqa: BLE001 - unparseable is not local
        return False
    return host in ("localhost", "127.0.0.1", "::1")


class OAuthPlugin:
    """Refresh-pair routes + OAuth2 code-flow routes over DatabasePlugin.

    Register AFTER `DatabasePlugin` (enforced: `requires = ["database"]`)::
        app.register(DatabasePlugin("sqlite:///app.db"))
        app.register(OAuthPlugin(
            auth_secret="...",
            provider=github_provider("id", "secret"),
            on_user=find_or_create,   # async (provider, info) -> sub
            allowed_redirect_uris=["https://app.example/oauth/callback"],
        ))

    Industry rules enforced: PKCE S256 by default (`use_pkce=False` only
    for legacy providers), single-use login state (replay fails), exact
    redirect allowlist when configured, HTTPS redirect_uris except
    localhost, RFC 6749 error codes on `/oauth/token`, `no-store` on all
    token responses.
    """

    name = "oauth"
    requires = ["database"]
    priority = 20

    def __init__(
        self,
        auth_secret: str,
        provider: OAuthProvider | None = None,
        on_user: Any = None,
        access_ttl: int = 900,
        refresh_ttl: int = 30 * 86400,
        use_pkce: bool = True,
        allowed_redirect_uris: list[str] | None = None,
    ):
        if not auth_secret or len(auth_secret) < 16:
            raise ValueError(
                "OAuthPlugin needs auth_secret= of 16+ chars (same secret that signs access JWTs)"
            )
        if provider is not None and on_user is None:
            raise ValueError("OAuthPlugin with a provider needs on_user= (async (provider, info) -> sub)")
        self.auth_secret = auth_secret
        self.provider = provider
        self.on_user = on_user
        self.store: RefreshStore | None = None
        self._db: Any = None
        self._access_ttl = access_ttl
        self._refresh_ttl = refresh_ttl
        self._use_pkce = use_pkce
        self._allowed = list(allowed_redirect_uris or [])

    def register(self, app: Any) -> None:
        async def _startup() -> None:
            db = getattr(app, "state_db", None)
            if db is None:
                raise RuntimeError("OAuthPlugin needs DatabasePlugin registered first")
            self._db = db
            self.store = RefreshStore(db, self._access_ttl, self._refresh_ttl)
            await self.store.ensure()
            await db.execute(
                f"CREATE TABLE IF NOT EXISTS {_PKCE_TABLE}"
                " (state_hash TEXT PRIMARY KEY, verifier TEXT, redirect_uri TEXT, exp REAL)"
            )
            app.state_oauth = self.store  # type: ignore

        app.on_startup(_startup)

        def _store() -> RefreshStore:
            if self.store is None or self._db is None:
                raise RuntimeError("OAuthPlugin store not ready: startup never ran (serve the app first)")
            return self.store

        def _check_redirect(uri: str) -> str:
            import urllib.parse

            uri = (uri or "").strip()
            if not uri:
                from ikarem import BadRequest

                raise BadRequest(
                    "pass ?redirect_uri= of your /oauth/callback (must match provider registration exactly)"
                )
            if self._allowed and uri not in self._allowed:
                from ikarem import BadRequest

                raise BadRequest(
                    f"redirect_uri {uri!r} not allowlisted: "
                    "add it to OAuthPlugin(allowed_redirect_uris=[...]) or fix the login link"
                )
            scheme = urllib.parse.urlsplit(uri).scheme.lower()
            if scheme != "https" and not _is_local(uri):
                from ikarem import BadRequest

                raise BadRequest(
                    f"redirect_uri {uri!r} must be https in production "
                    "(http leaks codes/tokens on the wire; localhost exempt for dev)"
                )
            return uri

        async def _remember_state(state: str, verifier: str | None, redirect_uri: str) -> None:
            now = time.time()
            await self._db.execute(f"DELETE FROM {_PKCE_TABLE} WHERE exp < ?", now)
            await self._db.execute(
                f"INSERT INTO {_PKCE_TABLE} (state_hash, verifier, redirect_uri, exp) VALUES (?, ?, ?, ?)",
                hashlib.sha256(state.encode()).hexdigest(),
                verifier or "",
                redirect_uri,
                now + 600,
            )

        async def _consume_state(state: str) -> tuple[str | None, str | None] | None:
            """Single-use: returns (verifier, redirect_uri), deletes the row.
            None = forged (never issued), replayed (already consumed), or stale."""
            row = await self._db.fetch_one(
                f"SELECT * FROM {_PKCE_TABLE} WHERE state_hash = ?",
                hashlib.sha256(state.encode()).hexdigest(),
            )
            if row is None:
                return None
            await self._db.execute(
                f"DELETE FROM {_PKCE_TABLE} WHERE state_hash = ?",
                hashlib.sha256(state.encode()).hexdigest(),
            )
            if float(row["exp"]) < time.time():
                return None
            return row.get("verifier") or None, row.get("redirect_uri")

        def _rfc_error(code: str, description: str, status: int = 400) -> Any:
            from ikarem import JSONResponse

            return JSONResponse({"error": code, "error_description": description}, status_code=status)

        @app.get("/oauth/login")
        async def oauth_login(req: Any) -> Any:
            from ikarem import RedirectResponse

            if self.provider is None:
                from ikarem import BadRequest

                raise BadRequest("no OAuth provider configured: OAuthPlugin(provider=...) wires /oauth/login")
            base = _check_redirect(str(req.query.get("redirect_uri", "")))
            state = sign_state(self.auth_secret)
            verifier, challenge = pkce_pair() if self._use_pkce else (None, None)
            await _remember_state(state, verifier, base)
            url = self.provider.login_url(base, state, code_challenge=challenge)
            return RedirectResponse(url, status_code=302)

        @app.get("/oauth/callback")
        async def oauth_callback(req: Any) -> dict:
            if self.provider is None or self.on_user is None:
                from ikarem import BadRequest

                raise BadRequest("no OAuth provider configured on this app")
            q = req.query
            if q.get("error"):
                from ikarem import BadRequest

                raise BadRequest(f"provider refused login ({q['error']}: {q.get('error_description', '')})")
            code = q.get("code", "")
            if not code:
                from ikarem import BadRequest

                raise BadRequest("callback without ?code=: start at /oauth/login, not here directly")
            state = q.get("state", "")
            try:
                verify_state(state, self.auth_secret)
            except ValueError as e:
                from ikarem import BadRequest

                raise BadRequest(f"login state invalid: {e}")
            consumed = await _consume_state(state)
            if consumed is None:
                # Forged (never issued), replayed (already consumed), or stale.
                from ikarem import BadRequest

                raise BadRequest("login state unknown or already used: restart login at /oauth/login")
            verifier, bound_uri = consumed
            base = _check_redirect(str(q.get("redirect_uri", "") or ""))
            if bound_uri and base != bound_uri:
                from ikarem import BadRequest

                raise BadRequest(
                    "callback redirect_uri differs from the login one: restart login at /oauth/login"
                )
            tokens = self.provider.exchange(code, base, code_verifier=verifier)
            info = self.provider.userinfo(tokens["access_token"])
            sub = self.on_user(self.provider.name, info)
            import inspect

            if inspect.isawaitable(sub):
                sub = await sub
            pair = await _store().issue(str(sub), self.auth_secret)
            from ikarem import JSONResponse

            return JSONResponse(pair, headers=dict(_NO_STORE))

        @app.post("/oauth/token")
        async def oauth_token(req: Any) -> dict:
            from ikarem import BadRequest

            try:
                body = await req.json()
            except Exception:
                body = {}
            if not isinstance(body, dict) or body.get("grant_type") != "refresh_token":
                raise BadRequest(
                    "POST {grant_type: 'refresh_token', refresh_token: '...'} "
                    "(password logins stay in your app; this endpoint only rotates)"
                )
            if not body.get("refresh_token"):
                return _rfc_error(
                    "invalid_request", "missing refresh_token: POST {grant_type, refresh_token}"
                )
            try:
                pair = await _store().rotate(body["refresh_token"], self.auth_secret)
            except LookupError as e:
                return _rfc_error("invalid_grant", str(e))
            except ValueError as e:
                return _rfc_error("invalid_grant", str(e))
            from ikarem import JSONResponse

            return JSONResponse(pair, headers=dict(_NO_STORE))

        @app.post("/oauth/revoke")
        async def oauth_revoke(req: Any) -> dict:
            try:
                body = await req.json()
            except Exception:
                body = {}
            token = body.get("refresh_token", "") if isinstance(body, dict) else ""
            if not token:
                from ikarem import BadRequest

                raise BadRequest("POST {refresh_token: '...'} to log out")
            return {"revoked": await _store().revoke(token)}


def _pair_response(pair: dict) -> dict:
    return pair
