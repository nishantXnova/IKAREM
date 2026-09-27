"""Common DatabaseConnector interface (Strategy pattern).

Framework code + user code program against this ABC. Each engine
(postgres/mysql/sqlite/mssql) is an interchangeable strategy conforming
to it. Drivers are lazy-imported inside implementations so the core
stays dependency-free.
"""

from __future__ import annotations

import abc
from typing import Any


class Transaction(abc.ABC):
    @abc.abstractmethod
    async def commit(self) -> None: ...

    @abc.abstractmethod
    async def rollback(self) -> None: ...

    async def __aenter__(self) -> "Transaction":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if exc[0] is None:
            await self.commit()
        else:
            await self.rollback()


class DatabaseConnector(abc.ABC):
    """Async connector every engine must implement."""

    dialect: str = "base"

    def __init__(self, url: str, **options: Any):
        self.url = url
        self.options = options
        self.connected = False

    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def disconnect(self) -> None: ...

    @abc.abstractmethod
    async def execute(self, query: str, *params: Any) -> Any: ...

    @abc.abstractmethod
    async def fetch_one(self, query: str, *params: Any) -> dict | None: ...

    @abc.abstractmethod
    async def fetch_all(self, query: str, *params: Any) -> list[dict]: ...

    async def execute_many(self, query: str, rows: list[tuple]) -> None:
        for row in rows:
            await self.execute(query, *row)

    @abc.abstractmethod
    def transaction(self) -> Transaction: ...

    async def __aenter__(self) -> "DatabaseConnector":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.disconnect()
