"""Connector factory: URL scheme -> strategy."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .base import DatabaseConnector


def parse_url(url: str) -> str:
    return urlparse(url).scheme.split("+")[0].lower()


def create_connector(url: str, **options: Any) -> DatabaseConnector:
    scheme = parse_url(url)
    if scheme in ("sqlite", "sqlite3"):
        from .sqlite_impl import SQLiteConnector

        return SQLiteConnector(url, **options)
    if scheme in ("postgres", "postgresql", "postgresql+asyncpg"):
        from .postgres import PostgresConnector

        return PostgresConnector(url, **options)
    if scheme in ("mysql", "mysql+aiomysql"):
        from .mysql import MySQLConnector

        return MySQLConnector(url, **options)
    if scheme in ("mssql", "sqlserver", "mssql+pyodbc", "mssql+aioodbc"):
        from .sqlserver import SQLServerConnector

        return SQLServerConnector(url, **options)
    raise ValueError(f"Unsupported database URL scheme: {scheme!r}")
