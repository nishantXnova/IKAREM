"""Opaque refresh tokens: rotation, reuse detection, revocation.

Core `ikarem` mints short-lived access JWTs and stops there. This module
is the long-lived half: random refresh tokens (stored hashed, like API
keys), single-use rotation, abuse handling (a reused rotated token kills
the whole chain), explicit revocation for logout.

Storage is one portable table (`token TEXT PRIMARY KEY` — no
autoincrement, so the same DDL runs on SQLite/Postgres/MySQL) behind the
app's `DatabasePlugin`. Placeholders stay `?`, rows are dicts.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

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
            " sub TEXT NOT NULL, expires_at REAL NOT NULL,"
            " revoked INTEGER NOT NULL DEFAULT 0, replaced_by TEXT,"
            " created_at REAL NOT NULL)"
        )

    def _pair(self, sub: str, auth_secret: str, **extra: Any) -> dict:
        from ikarem import create_token

        refresh = secrets.token_urlsafe(32)
        access = create_token(sub, auth_secret, expires_in=self.access_ttl, **extra)
        return {"access_token": access, "refresh_token": refresh, "expires_in": self.access_ttl}

    async def issue(self, sub: str, auth_secret: str, **extra: Any) -> dict:
        """Mint an access+refresh pair (call after password/OAuth login)."""
        pair = self._pair(sub, auth_secret, **extra)
        now = time.time()
        await self.db.execute(
            f"INSERT INTO {_TABLE} (token, sub, expires_at, revoked, created_at) VALUES (?, ?, ?, 0, ?)",
            _hash(pair["refresh_token"]),
            sub,
            now + self.refresh_ttl,
            now,
        )
        return pair

    async def rotate(self, refresh_token: str, auth_secret: str) -> dict:
        """Trade a refresh token for a new pair. Single-use: the old token
        dies. Presenting an already-rotated token means theft: the whole
        chain for that user is revoked and the call fails loudly."""
        row = await self.db.fetch_one(f"SELECT * FROM {_TABLE} WHERE token = ?", _hash(refresh_token))
        if row is None:
            raise LookupError("unknown refresh token: log in again (POST /login or /oauth/login)")
        if row.get("revoked"):
            if row.get("replaced_by"):
                await self.db.execute(f"UPDATE {_TABLE} SET revoked = 1 WHERE sub = ?", row["sub"])
                raise ValueError(
                    "refresh token reuse detected: possible theft, all sessions revoked. Log in again."
                )
            raise ValueError("refresh token revoked: log in again (POST /login or /oauth/login)")
        if float(row["expires_at"]) < time.time():
            raise ValueError("refresh token expired: log in again (POST /login or /oauth/login)")
        pair = self._pair(row["sub"], auth_secret)
        now = time.time()
        await self.db.execute(
            f"UPDATE {_TABLE} SET revoked = 1, replaced_by = ? WHERE token = ?",
            _hash(pair["refresh_token"]),
            _hash(refresh_token),
        )
        await self.db.execute(
            f"INSERT INTO {_TABLE} (token, sub, expires_at, revoked, created_at) VALUES (?, ?, ?, 0, ?)",
            _hash(pair["refresh_token"]),
            row["sub"],
            now + self.refresh_ttl,
            now,
        )
        return pair

    async def revoke(self, refresh_token: str) -> bool:
        """Revoke one token (logout). True when something was revoked."""
        row = await self.db.fetch_one(f"SELECT * FROM {_TABLE} WHERE token = ?", _hash(refresh_token))
        if row is None:
            return False
        await self.db.execute(f"UPDATE {_TABLE} SET revoked = 1 WHERE token = ?", _hash(refresh_token))
        return True

    async def revoke_all(self, sub: str) -> int:
        """Revoke every token for a user (password change, breach)."""
        await self.db.execute(f"UPDATE {_TABLE} SET revoked = 1 WHERE sub = ?", sub)
        return 1
