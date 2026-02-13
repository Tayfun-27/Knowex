# backend/app/database_connectors/mssql_connector.py

import pyodbc
from typing import List, Dict, Any, Optional
from app.database_connectors.base import BaseDatabaseConnector

class MSSQLConnector(BaseDatabaseConnector):
    def __init__(self):
        self.connection = None
        self.connection_string = None
    
    def connect(self, connection_string: str, **kwargs) -> bool:
        try:
            # SSL Sertifikası yoksa bağlantının reddedilmemesi için TrustServerCertificate ekliyoruz
            if "TrustServerCertificate" not in connection_string:
                if not connection_string.endswith(';'):
                    connection_string += ';'
                connection_string += "TrustServerCertificate=yes;"
            
            self.connection_string = connection_string
            # Cloud Run ve Tünel trafiği için timeout süresini 15 saniye tutuyoruz
            self.connection = pyodbc.connect(connection_string, timeout=15)
            return True
        except Exception as e:
            print(f"MSSQL bağlantı hatası: {e}")
            return False
    
    def test_connection(self) -> bool:
        if not self.connection:
            return False
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            return True
        except Exception as e:
            print(f"MSSQL bağlantı testi hatası: {e}")
            return False
    
    def get_schema(self) -> Dict[str, Any]:
        """MSSQL şemasını analiz et"""
        if not self.connection:
            return {}
        schema = {"tables": [], "database_name": None}
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT DB_NAME()")
            schema["database_name"] = cursor.fetchone()[0]
            
            cursor.execute("""
                SELECT TABLE_SCHEMA, TABLE_NAME 
                FROM INFORMATION_SCHEMA.TABLES 
                WHERE TABLE_TYPE = 'BASE TABLE' 
                AND TABLE_SCHEMA NOT IN ('sys', 'information_schema')
            """)
            
            for table_schema, table_name in cursor.fetchall():
                cursor.execute("""
                    SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
                """, (table_schema, table_name))
                
                columns = [{"name": c[0], "type": c[1], "nullable": c[2] == "YES", "default": c[3]} 
                           for c in cursor.fetchall()]
                
                schema["tables"].append({"name": f"{table_schema}.{table_name}", "columns": columns})
            cursor.close()
        except Exception as e:
            print(f"MSSQL şema hatası: {e}")
        return schema

    def execute_query(self, query: str, params: Optional[Dict] = None) -> List[Dict[str, Any]]:
        if not self.connection: raise Exception("Bağlantı yok")
        cursor = self.connection.cursor()
        cursor.execute(query, tuple(params.values()) if params else ())
        cols = [column[0] for column in cursor.description]
        results = [dict(zip(cols, row)) for row in cursor.fetchall()]
        cursor.close()
        return results

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None