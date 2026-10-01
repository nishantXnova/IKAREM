"""Layered centralized configuration.

Precedence (lowest -> highest):
  1. defaults passed to Config()
  2. kwargs to Ikarem(...)
  3. dict/file load via .update() / .load_dict()
  4. environment variables with IKAREM_ prefix

Keeps config independent from individual components: everything reads
from app.config instead of holding its own settings.
"""

from __future__ import annotations

import os
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class Config(dict):
    prefix = "IKAREM_"

    def __init__(self, defaults: dict | None = None, **kwargs: Any):
        super().__init__()
        if defaults:
            self.update(defaults)
        self.update(kwargs)
        self._load_env()

    def _load_env(self) -> None:
        for k, v in os.environ.items():
            if k.startswith(self.prefix):
                # IKAREM_DEBUG -> debug ; IKAREM_DB_URL -> db_url
                key = k[len(self.prefix) :].lower()
                self[key] = self._coerce(v)

    @staticmethod
    def _coerce(v: str) -> Any:
        lv = v.lower()
        if lv in ("true", "1", "yes", "on"):
            return True
        if lv in ("false", "0", "no", "off"):
            return False
        try:
            return int(v)
        except ValueError:
            pass
        try:
            return float(v)
        except ValueError:
            pass
        return v

    def get(self, key: str, default: Any = None, cast: Callable[[Any], T] | None = None) -> Any:  # type: ignore[override]
        v = super().get(key, default)
        if v is None or cast is None:
            return v
        try:
            return cast(v)
        except Exception:
            return default

    def load_dict(self, data: dict) -> None:
        self.update(data)

    def load_nish(self, path: str) -> None:
        """Load a ``.nish`` config file at dict/file precedence (below env).

        Values arrive typed by the format (no string coercion like env).
        """
        from pathlib import Path

        from .nish import from_nish

        self.update(from_nish(Path(path).read_text(encoding="utf-8")))
