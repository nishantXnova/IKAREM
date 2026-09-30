"""Auth: stdlib JWT (HS256), pbkdf2 passwords, Bearer + RBAC Depends. No deps."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def create_token(sub: str, secret: str, expires_in: int = 3600, **claims: Any) -> str:
    header = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64e(json.dumps({"sub": sub, "exp": int(time.time()) + expires_in, **claims}).encode())
    sig = _b64e(hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def verify_token(token: str, secret: str, algorithms: tuple = ("HS256",)) -> dict:
    """Verify signature, algorithm, expiry, and required claims.

    Rejects algorithm-confusion attacks (e.g. alg=none / RS256 tokens
    presented to an HS256 verifier) instead of ignoring the header.
    """
    try:
        h, p, s = token.split(".")
    except ValueError:
        raise ValueError("malformed token")
    try:
        header = json.loads(_b64d(h))
    except Exception:
        raise ValueError("malformed token header")
    alg = header.get("alg")
    if alg not in algorithms:
        raise ValueError(f"unexpected algorithm {alg!r}")
    expect = _b64e(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expect, s):
        raise ValueError("bad signature")
    try:
        data = json.loads(_b64d(p))
    except Exception:
        raise ValueError("malformed token payload")
    if not isinstance(data, dict):
        raise ValueError("malformed token payload")
    if data.get("exp") and data["exp"] < time.time():
        raise ValueError("token expired")
    if "sub" not in data:
        raise ValueError("missing 'sub' claim")
    return data


def hash_password(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 210_000)
    return f"pbkdf2${salt}${dk.hex()}"


def check_password(pw: str, hashed: str) -> bool:
    try:
        _, salt, hexd = hashed.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(pw, salt), hashed)


class BearerAuth:
    """Dependency: reads Authorization: Bearer <jwt>, returns claims dict."""

    _ikarem_security_scheme = "bearer"

    def __init__(self, secret: str, required: bool = True):
        self.secret = secret
        self.required = required
        self._ikarem_security = {"scheme": "bearer", "roles": ()}

    async def __call__(self, request: Any) -> dict | None:
        from .errors import Unauthorized

        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            if self.required:
                raise Unauthorized("missing bearer token")
            return None
        try:
            return verify_token(auth[7:], self.secret)
        except Exception as e:
            if self.required:
                raise Unauthorized(str(e))
            return None


def require_roles(*roles: str):
    """Depends factory enforcing JWT `roles` claim."""

    async def _check(request: Any) -> dict:
        from .errors import Forbidden, Unauthorized

        auth = request.headers.get("authorization", "")
        secret = request.app.config.get("auth_secret", "change-me")
        if not auth.startswith("Bearer "):
            raise Unauthorized("missing bearer token")
        try:
            claims = verify_token(auth[7:], secret)
        except Exception as e:
            raise Unauthorized(str(e))
        have = set(claims.get("roles", []))
        if roles and not have.intersection(roles):
            raise Forbidden(f"requires one of {roles}")
        return claims

    _check._ikarem_security = {"scheme": "bearer", "roles": roles}  # type: ignore
    _check._ikarem_config_secret = True  # type: ignore  # secret comes from app config
    return _check


def require_scopes(*scopes: str):
    """Depends factory enforcing JWT `scope` (space-separated) or `scp` claim."""

    async def _check(request: Any) -> dict:
        from .errors import Forbidden, Unauthorized

        auth = request.headers.get("authorization", "")
        secret = request.app.config.get("auth_secret", "change-me")
        if not auth.startswith("Bearer "):
            raise Unauthorized("missing bearer token")
        try:
            claims = verify_token(auth[7:], secret)
        except Exception as e:
            raise Unauthorized(str(e))
        raw = claims.get("scope", claims.get("scp", ""))
        have = set(raw.split()) if isinstance(raw, str) else set(raw or [])
        missing = [s for s in scopes if s not in have]
        if missing:
            raise Forbidden(f"missing scopes: {missing}")
        return claims

    _check._ikarem_security = {"scheme": "bearer", "roles": (), "scopes": scopes}  # type: ignore
    return _check


def require_if(predicate: Any, detail: str = "forbidden"):
    """ABAC-lite: allow when ``predicate(claims)`` is truthy (JWT bearer).

    ``claims=Depends(require_if(lambda c: c.get("tenant") == "acme"))``.
    """

    async def _check(request: Any) -> dict:
        from .errors import Forbidden, Unauthorized

        auth = request.headers.get("authorization", "")
        secret = request.app.config.get("auth_secret", "change-me")
        if not auth.startswith("Bearer "):
            raise Unauthorized("missing bearer token")
        try:
            claims = verify_token(auth[7:], secret)
        except Exception as e:
            raise Unauthorized(str(e))
        ok = predicate(claims)
        if hasattr(ok, "__await__"):
            ok = await ok
        if not ok:
            raise Forbidden(detail)
        return claims

    _check._ikarem_security = {"scheme": "bearer", "roles": ()}  # type: ignore
    return _check


class APIKeyAuth:
    """Dependency: static API keys via header (default ``X-API-Key``).

    ``APIKeyAuth({"service-a": "key-secret"})`` or pass ``lookup=`` — a
    sync/async callable ``key -> info dict`` raising/returning None when
    unknown. Returns the key's info (never the secret).
    """

    _ikarem_security_scheme = "apiKey"

    def __init__(
        self,
        keys: dict[str, Any] | None = None,
        lookup: Any = None,
        header: str = "x-api-key",
        required: bool = True,
    ):
        self.keys = keys or {}
        self.lookup = lookup
        self.header = header
        self.required = required
        self._ikarem_security = {"scheme": "apiKey", "roles": (), "header": header}

    async def __call__(self, request: Any) -> Any | None:
        import inspect

        from .errors import Unauthorized

        key = request.headers.get(self.header.lower(), "")
        info: Any = None
        if key:
            if self.lookup is not None:
                info = self.lookup(key)
                if inspect.isawaitable(info):
                    info = await info
            else:
                info = self.keys.get(key)
        if info is None and self.required:
            raise Unauthorized("invalid or missing API key")
        return info
