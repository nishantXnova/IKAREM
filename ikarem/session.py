"""Browser-grade sessions + CSRF. Zero deps (stdlib hmac/sha256/json only).

SessionMiddleware: tamper-proof signed-cookie sessions. Handlers use
`req.session` like a dict; login = `req.session["uid"] = ...`,
logout = `req.session.clear()`. Expiry enforced, constant-time verify.

CSRFMiddleware (requires SessionMiddleware before it): double-submit tokens.
Safe methods mint/refresh the token (`csrf_token(req)` for templates);
unsafe methods must echo it via `X-CSRF-Token` header or `_csrf_token`
form field. API-only routes can opt out via exempt_paths.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from .http import JSONResponse
from .middleware import Middleware


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Session(dict):
    """Session mapping; any mutation marks it dirty for saving."""

    def __init__(self, data: dict | None = None):
        super().__init__(data or {})
        self.modified = False

    def _touch(self) -> None:
        self.modified = True

    def __setitem__(self, k: Any, v: Any) -> None:
        self._touch()
        super().__setitem__(k, v)

    def __delitem__(self, k: Any) -> None:
        self._touch()
        super().__delitem__(k)

    def clear(self) -> None:
        self._touch()
        super().clear()

    def pop(self, *a: Any, **k: Any) -> Any:
        self._touch()
        return super().pop(*a, **k)

    def popitem(self) -> Any:
        self._touch()
        return super().popitem()

    def setdefault(self, *a: Any, **k: Any) -> Any:
        self._touch()
        return super().setdefault(*a, **k)

    def update(self, *a: Any, **k: Any) -> None:
        self._touch()
        super().update(*a, **k)


def _sign(secret: str, payload_b64: str) -> str:
    return _b64e(hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest())


def encode_session(data: dict, secret: str, max_age: int) -> str:
    now = int(time.time())
    payload = _b64e(
        json.dumps(
            {"data": data, "iat": now, "exp": now + max_age},
            separators=(",", ":"),
        ).encode()
    )
    return f"{payload}.{_sign(secret, payload)}"


def decode_session(value: str, secret: str) -> dict | None:
    try:
        payload_b64, sig = value.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(secret, payload_b64), sig):
        return None
    try:
        payload = json.loads(_b64d(payload_b64))
    except Exception:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    return payload["data"]


class SessionMiddleware(Middleware):
    """Signed-cookie sessions. Reads secret from arg or app config
    (session_secret, falling back to auth_secret)."""

    def __init__(
        self,
        secret: str | None = None,
        cookie_name: str = "ikarem_session",
        max_age: int = 7 * 24 * 3600,
        same_site: str = "Lax",
        secure: bool = False,
    ):
        self.secret = secret
        self.cookie_name = cookie_name
        self.max_age = max_age
        self.same_site = same_site
        self.secure = secure

    def _secret(self, req: Any) -> str:
        if self.secret:
            return self.secret
        cfg = getattr(getattr(req, "app", None), "config", {})
        try:
            secret = cfg.get("session_secret") or cfg.get("auth_secret")
        except Exception:
            secret = None
        if not secret or secret in ("change-me",):
            raise RuntimeError(
                "SessionMiddleware needs a secret: pass secret= or set "
                "session_secret (Ikarem(session_secret=...) / IKAREM_SESSION_SECRET)"
            )
        return secret

    async def __call__(self, req: Any, call_next: Any) -> Any:
        secret = self._secret(req)
        data = decode_session(req.cookies.get(self.cookie_name, ""), secret) or {}
        req.session = Session(data)
        resp = await call_next(req)
        if req.session.modified:
            if len(req.session) == 0:
                resp.delete_cookie(self.cookie_name)
            else:
                resp.set_cookie(
                    self.cookie_name,
                    encode_session(dict(req.session), secret, self.max_age),
                    max_age=self.max_age,
                    httponly=True,
                    samesite=self.same_site,
                    secure=self.secure,
                )
        return resp


CSRF_FIELD = "_csrf_token"
CSRF_HEADER = "x-csrf-token"
CSRF_SESSION_KEY = "_csrf"


def csrf_token(req: Any) -> str:
    """Token for templates/hidden fields. Ensures one exists in session."""
    sess = getattr(req, "session", None)
    if sess is None:
        raise RuntimeError("csrf_token(req) requires SessionMiddleware")
    tok = sess.get(CSRF_SESSION_KEY)
    if not tok:
        tok = secrets.token_urlsafe(32)
        sess[CSRF_SESSION_KEY] = tok
    return tok


class CSRFMiddleware(Middleware):
    """Double-submit CSRF: unsafe methods need X-CSRF-Token header or
    _csrf_token form field matching the session token. Requires
    SessionMiddleware earlier in the stack. exempt_paths skips API routes."""

    SAFE = {"GET", "HEAD", "OPTIONS"}

    def __init__(self, exempt_paths: list[str] | None = None):
        self.exempt_paths = exempt_paths or []

    def _exempt(self, path: str) -> bool:
        for p in self.exempt_paths:
            if p.endswith("*"):
                if path.startswith(p[:-1]):
                    return True
            elif path == p:
                return True
        return False

    async def __call__(self, req: Any, call_next: Any) -> Any:
        sess = getattr(req, "session", None)
        if sess is None:
            raise RuntimeError("CSRFMiddleware requires SessionMiddleware before it")
        if self._exempt(req.path):
            return await call_next(req)
        if req.method in self.SAFE:
            csrf_token(req)  # mint for templates
            return await call_next(req)
        expect = sess.get(CSRF_SESSION_KEY, "")
        got = req.headers.get(CSRF_HEADER, "")
        if not got and req.headers.get("content-type", "").split(";")[0].strip().lower() in (
            "application/x-www-form-urlencoded",
            "multipart/form-data",
        ):
            try:
                got = (await req.form()).get(CSRF_FIELD, "")
            except Exception:
                got = ""
        if not expect or not got or not hmac.compare_digest(str(expect), str(got)):
            return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)
        return await call_next(req)
