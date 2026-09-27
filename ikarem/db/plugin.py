"""DatabasePlugin — validates the plugin architecture with a real extension.

Usage:
    from ikarem.db import DatabasePlugin
    app.register(DatabasePlugin("sqlite:///:memory:"))
    # handler: await request.app.state_db.fetch_all("SELECT ...")
"""

from __future__ import annotations

from typing import Any

from ..plugins import BasePlugin
from .factory import create_connector


class DatabasePlugin(BasePlugin):
    name = "database"
    priority = 10

    def __init__(self, url: str, **options: Any):
        self.url = url
        self.options = options
        self.db: Any = None

    def register(self, app: Any) -> None:
        self.db = create_connector(self.url, **self.options)
        app.config.load_dict({"db_url": self.url})

        async def _startup() -> None:
            await self.db.connect()
            app.state_db = self.db  # type: ignore

        async def _shutdown() -> None:
            await self.db.disconnect()

        app.on_startup(_startup)
        app.on_shutdown(_shutdown)
