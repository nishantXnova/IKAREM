"""OAuth2 authorization-code login: generic provider + Google/GitHub presets.

The app never sees passwords (or sees them once, for local users): the
provider authenticates, we verify a signed `state` (CSRF), exchange the
code, read userinfo, and hand the app's `on_user` hook the profile. The
hook returns the local `sub` (find-or-create — your users table, your
rules) and the caller gets an IKAREM access+refresh pair.

Provider HTTP is injectable (`http_post`/`http_get`) so tests run with
fakes and zero network. Production defaults use stdlib `urllib` only —
no `requests`, no new dependency.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
import urllib.request
from typing import Any


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_state(auth_secret: str, ttl: int = 600) -> str:
    payload = _b64e(json.dumps({"n": secrets.token_hex(8), "exp": time.time() + ttl}).encode())
    sig = _b64e(hmac.new(auth_secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_state(state: str, auth_secret: str) -> None:
    """Raise with the fix when state is forged, tampered, or stale."""
    try:
        payload, sig = state.split(".", 1)
        expect = _b64e(hmac.new(auth_secret.encode(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(expect, sig):
            raise ValueError("bad signature")
        if json.loads(_b64d(payload).decode())["exp"] < time.time():
            raise ValueError("expired")
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001 - malformed state is a login restart, not a 500
        raise ValueError(f"malformed state ({e}): restart login at /oauth/login") from e


def _urllib_post(url: str, data: dict, headers: dict | None = None) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
    except Exception as e:  # noqa: BLE001 - network errors become 502s with the provider named
        raise RuntimeError(f"provider token exchange failed ({url}): {e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return dict(urllib.parse.parse_qsl(raw))


def _urllib_get(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"provider userinfo fetch failed ({url}): {e}") from e


class OAuthProvider:
    """One OAuth2 provider. Subclass nothing — configure URLs + transport."""

    def __init__(
        self,
        name: str,
        authorize_url: str,
        token_url: str,
        userinfo_url: str,
        client_id: str,
        client_secret: str,
        scopes: list[str] | None = None,
        http_post: Any = None,
        http_get: Any = None,
    ):
        if not client_id or not client_secret:
            raise ValueError(
                f"OAuth provider {name!r} needs client_id= and client_secret= "
                "(register an OAuth app at the provider, then pass both)"
            )
        self.name = name
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.userinfo_url = userinfo_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes or []
        self.http_post = http_post or _urllib_post
        self.http_get = http_get or _urllib_get

    def login_url(self, redirect_uri: str, state: str) -> str:
        q = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
        }
        return f"{self.authorize_url}?{urllib.parse.urlencode(q)}"

    def exchange(self, code: str, redirect_uri: str) -> dict:
        data = self.http_post(
            self.token_url,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        if not isinstance(data, dict) or "access_token" not in data:
            raise RuntimeError(
                f"provider {self.name} refused the code exchange "
                f"({data.get('error', data) if isinstance(data, dict) else data}): "
                "check client_id/secret and that redirect_uri matches registration exactly"
            )
        return data

    def userinfo(self, access_token: str) -> dict:
        info = self.http_get(self.userinfo_url, access_token)
        if not isinstance(info, dict):
            raise RuntimeError(f"provider {self.name} returned unusable userinfo: {info!r}")
        return info


def github_provider(client_id: str, client_secret: str, **kw: Any) -> OAuthProvider:
    return OAuthProvider(
        "github",
        "https://github.com/login/oauth/authorize",
        "https://github.com/login/oauth/access_token",
        "https://api.github.com/user",
        client_id,
        client_secret,
        scopes=kw.pop("scopes", ["read:user", "user:email"]),
        **kw,
    )


def google_provider(client_id: str, client_secret: str, **kw: Any) -> OAuthProvider:
    return OAuthProvider(
        "google",
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        "https://www.googleapis.com/oauth2/v3/userinfo",
        client_id,
        client_secret,
        scopes=kw.pop("scopes", ["openid", "email", "profile"]),
        **kw,
    )
