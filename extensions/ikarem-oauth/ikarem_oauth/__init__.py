"""ikarem-oauth: refresh tokens + OAuth2 login for IKAREM apps."""

from __future__ import annotations

from typing import Any

from .flow import OAuthProvider, github_provider, google_provider, sign_state, verify_state
from .tokens import RefreshStore

__all__ = [
    "OAuthPlugin",
    "OAuthProvider",
    "RefreshStore",
    "github_provider",
    "google_provider",
    "sign_state",
    "verify_state",
]


class OAuthPlugin:
    """Refresh-pair routes + OAuth2 code-flow routes over DatabasePlugin.

    Register AFTER `DatabasePlugin` (enforced: `requires = ["database"]`)::
        app.register(DatabasePlugin("sqlite:///app.db"))
        app.register(OAuthPlugin(
            auth_secret="...",
            provider=github_provider("id", "secret"),
            on_user=find_or_create,   # async (provider, info) -> sub
        ))
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
        self._access_ttl = access_ttl
        self._refresh_ttl = refresh_ttl

    def register(self, app: Any) -> None:
        async def _startup() -> None:
            db = getattr(app, "state_db", None)
            if db is None:
                raise RuntimeError("OAuthPlugin needs DatabasePlugin registered first")
            self.store = RefreshStore(db, self._access_ttl, self._refresh_ttl)
            await self.store.ensure()
            app.state_oauth = self.store  # type: ignore

        app.on_startup(_startup)

        def _store() -> RefreshStore:
            if self.store is None:
                raise RuntimeError("OAuthPlugin store not ready: startup never ran (serve the app first)")
            return self.store

        @app.get("/oauth/login")
        async def oauth_login(req: Any) -> Any:
            from ikarem import RedirectResponse

            if self.provider is None:
                from ikarem import BadRequest

                raise BadRequest("no OAuth provider configured: OAuthPlugin(provider=...) wires /oauth/login")
            base = str(req.query.get("redirect_uri", "") or "").strip()
            if not base:
                from ikarem import BadRequest

                raise BadRequest(
                    "pass ?redirect_uri= of your /oauth/callback (must match provider registration)"
                )
            url = self.provider.login_url(base, sign_state(self.auth_secret))
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
            try:
                verify_state(q.get("state", ""), self.auth_secret)
            except ValueError as e:
                from ikarem import BadRequest

                raise BadRequest(f"login state invalid: {e}")
            base = str(q.get("redirect_uri", "") or "").strip() or "/oauth/callback"
            tokens = self.provider.exchange(code, base)
            info = self.provider.userinfo(tokens["access_token"])
            sub = self.on_user(self.provider.name, info)
            import inspect

            if inspect.isawaitable(sub):
                sub = await sub
            return await _store().issue(str(sub), self.auth_secret)

        @app.post("/oauth/token")
        async def oauth_token(req: Any) -> dict:
            from ikarem import BadRequest

            try:
                body = await req.json()
            except Exception:
                body = {}
            if (
                not isinstance(body, dict)
                or body.get("grant_type") != "refresh_token"
                or not body.get("refresh_token")
            ):
                raise BadRequest(
                    "POST {grant_type: 'refresh_token', refresh_token: '...'} "
                    "(password logins stay in your app; this endpoint only rotates)"
                )
            try:
                return await _store().rotate(body["refresh_token"], self.auth_secret)
            except (LookupError, ValueError) as e:
                from ikarem import Unauthorized

                raise Unauthorized(str(e))

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
