# backend/app/database_connectors/__init__.py

from app.database_connectors.base import BaseDatabaseConnector
from app.database_connectors.postgres_connector import PostgreSQLConnector
from app.database_connectors.mongodb_connector import MongoDBConnector
from app.database_connectors.mysql_connector import MySQLConnector
from app.database_connectors.mssql_connector import MSSQLConnector


def get_database_connector(db_type: str) -> BaseDatabaseConnector:
    db_type = (db_type or "").lower()

    if db_type in ("postgresql", "postgres"):
        return PostgreSQLConnector()
    if db_type in ("mongodb", "mongo"):
        return MongoDBConnector()
    if db_type == "mysql":
        return MySQLConnector()
    if db_type in ("mssql", "sqlserver", "sql_server"):
        return MSSQLConnector()

    raise ValueError(f"Desteklenmeyen veritabanı tipi: {db_type}")


__all__ = [
    "BaseDatabaseConnector",
    "PostgreSQLConnector",
    "MongoDBConnector",
    "MySQLConnector",
    "MSSQLConnector",
    "get_database_connector",
]
