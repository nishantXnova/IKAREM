"""Database extension: Strategy-pattern connector + per-engine implementations."""

from .base import DatabaseConnector, Transaction
from .factory import create_connector, parse_url
from .mysql import MySQLConnector
from .plugin import DatabasePlugin
from .postgres import PostgresConnector
from .sqlite_impl import SQLiteConnector
from .sqlserver import SQLServerConnector

__all__ = [
    "DatabaseConnector",
    "Transaction",
    "DatabasePlugin",
    "SQLiteConnector",
    "PostgresConnector",
    "MySQLConnector",
    "SQLServerConnector",
    "create_connector",
    "parse_url",
]
