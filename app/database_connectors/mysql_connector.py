# backend/app/database_connectors/mysql_connector.py

from __future__ import annotations

import os
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, unquote

from app.database_connectors.base import BaseDatabaseConnector


class MySQLConnector(BaseDatabaseConnector):
    """
    BaseDatabaseConnector interface'ine uyumlu MySQL connector.

    connect(connection_string) bekler.
    Desteklenen connection string örnekleri:
      - mysql://user:pass@host:3306/dbname
      - mysql+pymysql://user:pass@host:3306/dbname   (scheme mysql ile başlıyorsa kabul)
      - host=...;port=...;user=...;password=...;database=...  (kv format)
    """

    def __init__(self):
        self.connection = None
        self.connection_string: Optional[str] = None
        self._driver = None
        self._mysql_connector = None
        self._pymysql = None
        self._load_driver()

    def _load_driver(self) -> None:
        # 1) mysql-connector-python
        try:
            import mysql.connector  # type: ignore
            self._driver = "mysql_connector"
            self._mysql_connector = mysql.connector
            return
        except Exception:
            pass

        # 2) PyMySQL
        try:
            import pymysql  # type: ignore
            self._driver = "pymysql"
            self._pymysql = pymysql
            return
        except Exception as e:
            raise RuntimeError(
                "MySQL driver bulunamadı. requirements'a mysql-connector-python veya pymysql ekleyin."
            ) from e

    def _parse_connection_string(self, cs: str) -> Dict[str, Any]:
        cs = (cs or "").strip()

        # URL formatı
        if "://" in cs:
            u = urlparse(cs)
            if not u.scheme.startswith("mysql"):
                raise ValueError(f"MySQL scheme desteklenmiyor: {u.scheme}")

            host = u.hostname or ""
            port = int(u.port or 3306)
            user = unquote(u.username or "")
            password = unquote(u.password or "")
            database = (u.path or "").lstrip("/") or None

            if not host:
                raise ValueError("MySQL connection string içinde host bulunamadı.")

            return {"host": host, "port": port, "user": user, "password": password, "database": database}

        # key=value;key=value formatı
        if ";" in cs and "=" in cs:
            kv: Dict[str, str] = {}
            for p in [x.strip() for x in cs.split(";") if x.strip()]:
                if "=" not in p:
                    continue
                k, v = p.split("=", 1)
                kv[k.strip().lower()] = v.strip()

            host = kv.get("host") or kv.get("server") or kv.get("hostname") or ""
            port = int(kv.get("port") or "3306")
            user = kv.get("user") or kv.get("uid") or kv.get("username") or ""
            password = kv.get("password") or kv.get("pwd") or ""
            database = kv.get("database") or kv.get("db") or None

            if not host:
                raise ValueError("MySQL connection string içinde host bulunamadı (key=value).")

            return {"host": host, "port": port, "user": user, "password": password, "database": database}

        # ENV fallback (opsiyonel)
        host = os.getenv("MYSQL_HOST", "")
        if not host:
            raise ValueError("MySQL bağlantı bilgisi yok: connection_string boş ve MYSQL_HOST env yok.")

        return {
            "host": host,
            "port": int(os.getenv("MYSQL_PORT", "3306")),
            "user": os.getenv("MYSQL_USER", ""),
            "password": os.getenv("MYSQL_PASSWORD", ""),
            "database": os.getenv("MYSQL_DATABASE") or None,
        }

    def connect(self, connection_string: str, **kwargs) -> bool:
        try:
            self.connection_string = connection_string
            cfg = self._parse_connection_string(connection_string)

            connect_timeout = int(kwargs.get("connect_timeout", 10))

            if self._driver == "mysql_connector":
                self.connection = self._mysql_connector.connect(
                    host=cfg["host"],
                    port=cfg["port"],
                    user=cfg["user"],
                    password=cfg["password"],
                    database=cfg["database"],
                    connection_timeout=connect_timeout,
                    autocommit=True,
                )
                return True

            # pymysql
            self.connection = self._pymysql.connect(
                host=cfg["host"],
                port=cfg["port"],
                user=cfg["user"],
                password=cfg["password"],
                database=cfg["database"],
                connect_timeout=connect_timeout,
                read_timeout=int(kwargs.get("read_timeout", 30)),
                write_timeout=int(kwargs.get("write_timeout", 30)),
                autocommit=True,
                cursorclass=self._pymysql.cursors.DictCursor,
            )
            return True

        except Exception as e:
            print(f"MySQL bağlantı hatası: {e}")
            self.connection = None
            return False

    def test_connection(self) -> bool:
        if not self.connection:
            return False
        try:
            cur = self._cursor()
            cur.execute("SELECT 1")
            cur.close()
            return True
        except Exception as e:
            print(f"MySQL bağlantı testi hatası: {e}")
            return False

    def _cursor(self):
        if self._driver == "mysql_connector":
            return self.connection.cursor(dictionary=True)
        return self.connection.cursor()

    def execute_query(self, query: str, params: Optional[Dict] = None) -> List[Dict[str, Any]]:
        if not self.connection:
            raise RuntimeError("MySQL bağlantısı yok. Önce connect() çağır.")

        cur = self._cursor()
        try:
            cur.execute(query, params or {})
            if cur.description:  # SELECT
                rows = cur.fetchall() or []
                return [dict(r) for r in rows]
            return []
        except Exception as e:
            print(f"MySQL SQL sorgu hatası: {e}")
            raise
        finally:
            cur.close()

    def get_schema(self) -> Dict[str, Any]:
        if not self.connection:
            return {}

        try:
            schema: Dict[str, Any] = {"tables": []}

            db_rows = self.execute_query("SELECT DATABASE() AS db")
            schema["database_name"] = (db_rows[0].get("db") if db_rows else None)

            tables = self.execute_query(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                ORDER BY table_name
                """
            )

            for t in tables:
                table_name = t.get("table_name") or t.get("TABLE_NAME")
                if not table_name:
                    continue

                cols = self.execute_query(
                    """
                    SELECT column_name, data_type, is_nullable, column_key
                    FROM information_schema.columns
                    WHERE table_schema = DATABASE() AND table_name = %(table)s
                    ORDER BY ordinal_position
                    """,
                    {"table": table_name},
                )

                schema["tables"].append(
                    {
                        "name": table_name,
                        "columns": [
                            {
                                "name": c.get("column_name") or c.get("COLUMN_NAME"),
                                "type": c.get("data_type") or c.get("DATA_TYPE"),
                                "nullable": (c.get("is_nullable") or c.get("IS_NULLABLE")) == "YES",
                                "key": c.get("column_key") or c.get("COLUMN_KEY"),
                            }
                            for c in cols
                        ],
                    }
                )

            return schema

        except Exception as e:
            print(f"MySQL şema okuma hatası: {e}")
            return {}

    def close(self):
        try:
            if self.connection:
                self.connection.close()
        finally:
            self.connection = None
