"""Opaque refresh tokens: rotation, reuse detection, revocation.

Core `ikarem` mints short-lived access JWTs and stops there. This module
is the long-lived half: random refresh tokens (stored hashed, like API
keys), single-use rotation, abuse handling (a reused rotated token kills
the whole chain), explicit revocation for logout.

Storage is portable (`token TEXT PRIMARY KEY` — no autoincrement, same
DDL on SQLite/Postgres/MySQL) behind the app's `DatabasePlugin`.
Placeholders stay `?`, rows are dicts. Every lifecycle event logs on
`ikarem_oauth` (SOC2 wants an auth trail: issue/rotate/revoke/reuse).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Any

log = logging.getLogger("ikarem_oauth")

_TABLE = "ikarem_oauth_refresh"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RefreshStore:
    """Refresh-token lifecycle over any IKAREM `DatabaseConnector`."""

    def __init__(self, db: Any, access_ttl: int = 900, refresh_ttl: int = 30 * 86400):
        self.db = db
        self.access_ttl = access_ttl
        self.refresh_ttl = refresh_ttl

    async def ensure(self) -> None:
        await self.db.execute(
            f"CREATE TABLE IF NOT EXISTS {_TABLE} (token TEXT PRIMARY KEY,"
            " sub TEXT NOT NULL, scopes TEXT NOT NULL DEFAULT '', expires_at REAL NOT NULL,"
            " revoked INTEGER NOT NULL DEFAULT 0, replaced_by TEXT,"
            " created_at REAL NOT NULL)"
        )
        try:  # older installs lack the column; fresh tables already have it
            await self.db.execute(f"ALTER TABLE {_TABLE} ADD COLUMN scopes TEXT NOT NULL DEFAULT ''")
        except Exception:  # noqa: BLE001
            pass

    def _pair(self, sub: str, auth_secret: str, scopes: list[str], **extra: Any) -> dict:
        from ikarem import create_token

        refresh = secrets.token_urlsafe(32)
        access = create_token(
            sub,
            auth_secret,
            expires_in=self.access_ttl,
            jti=secrets.token_hex(8),
            scopes=sorted(scopes),
            **extra,
        )
        return {
            "access_token": access,
            "token_type": "Bearer",
            "refresh_token": refresh,
            "expires_in": self.access_ttl,
        }

    async def issue(self, sub: str, auth_secret: str, scopes: list[str] | None = None, **extra: Any) -> dict:
        """Mint an access+refresh pair (call after password/OAuth login)."""
        pair = self._pair(sub, auth_secret, sorted(scopes or []), **extra)
        now = time.time()
        await self.db.execute(
            f"INSERT INTO {_TABLE} (token, sub, scopes, expires_at, revoked, created_at) VALUES (?, ?, ?, ?, 0, ?)",
            _hash(pair["refresh_token"]),
            sub,
            " ".join(sorted(scopes or [])),
            now + self.refresh_ttl,
            now,
        )
        log.info("oauth.issue sub=%s", sub)
        return pair

    def _scopes_of(self, row: Any) -> list[str]:
        return [s for s in str(row.get("scopes") or "").split(" ") if s]

    async def rotate(self, refresh_token: str, auth_secret: str) -> dict:
        """Trade a refresh token for a new pair. Single-use: the old token
        dies. Presenting an already-rotated token means theft: the whole
        chain for that user is revoked and the call fails loudly."""
        row = await self.db.fetch_one(f"SELECT * FROM {_TABLE} WHERE token = ?", _hash(refresh_token))
        if row is None:
            log.warning("oauth.rotate unknown token")
            raise LookupError("unknown refresh token: log in again (POST /login or /oauth/login)")
        if row.get("revoked"):
            if row.get("replaced_by"):
                await self.db.execute(f"UPDATE {_TABLE} SET revoked = 1 WHERE sub = ?", row["sub"])
                log.warning("oauth.reuse-detected sub=%s: chain revoked", row["sub"])
                raise ValueError(
                    "refresh token reuse detected: possible theft, all sessions revoked. Log in again."
                )
            raise ValueError("refresh token revoked: log in again (POST /login or /oauth/login)")
        if float(row["expires_at"]) < time.time():
            raise ValueError("refresh token expired: log in again (POST /login or /oauth/login)")
        scopes = self._scopes_of(row)
        pair = self._pair(row["sub"], auth_secret, scopes)
        now = time.time()
        await self.db.execute(
            f"UPDATE {_TABLE} SET revoked = 1, replaced_by = ? WHERE token = ?",
            _hash(pair["refresh_token"]),
            _hash(refresh_token),
        )
        await self.db.execute(
            f"INSERT INTO {_TABLE} (token, sub, scopes, expires_at, revoked, created_at) VALUES (?, ?, ?, ?, 0, ?)",
            _hash(pair["refresh_token"]),
            row["sub"],
            " ".join(scopes),
            now + self.refresh_ttl,
            now,
        )
        log.info("oauth.rotate sub=%s", row["sub"])
        return pair

    async def revoke(self, refresh_token: str) -> bool:
        """Revoke one token (logout). True when something was revoked."""
        row = await self.db.fetch_one(f"SELECT * FROM {_TABLE} WHERE token = ?", _hash(refresh_token))
        if row is None:
            return False
        await self.db.execute(f"UPDATE {_TABLE} SET revoked = 1 WHERE token = ?", _hash(refresh_token))
        log.info("oauth.revoke sub=%s", row.get("sub"))
        return True

    async def revoke_all(self, sub: str) -> int:
        """Revoke every token for a user (password change, breach)."""
        await self.db.execute(f"UPDATE {_TABLE} SET revoked = 1 WHERE sub = ?", sub)
        log.info("oauth.revoke-all sub=%s", sub)
        return 1
