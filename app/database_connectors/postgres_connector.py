import psycopg2
from psycopg2.extras import RealDictCursor
from typing import List, Dict, Any, Optional
from app.database_connectors.base import BaseDatabaseConnector

class PostgreSQLConnector(BaseDatabaseConnector):
    def __init__(self):
        self.connection = None
        self.connection_string = None
    
    def connect(self, connection_string: str, **kwargs) -> bool:
        try:
            self.connection_string = connection_string
            # GÜNCELLEME: connect_timeout parametresi eklendi
            self.connection = psycopg2.connect(
                connection_string,
                connect_timeout=5,  # 5 saniye içinde bağlanamazsa işlemi iptal et
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=5
            )
            return True
        except Exception as e:
            print(f"PostgreSQL bağlantı hatası: {e}")
            return False
    
    # ... (Dosyanın geri kalanı aynı) ...
    def test_connection(self) -> bool:
        if not self.connection:
            return False
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            return True
        except Exception as e:
            print(f"PostgreSQL bağlantı testi hatası: {e}")
            return False
    
    def get_schema(self) -> Dict[str, Any]:
        """PostgreSQL şemasını analiz et"""
        if not self.connection:
            return {}
        
        schema = {
            "tables": [],
            "database_name": None
        }
        
        try:
            cursor = self.connection.cursor()
            
            # Database adını al
            cursor.execute("SELECT current_database()")
            schema["database_name"] = cursor.fetchone()[0]
            
            # Tabloları al
            cursor.execute("""
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY table_schema, table_name
            """)
            
            tables = cursor.fetchall()
            print(f"📊 PostgreSQL'den alınan tablolar: {len(tables)} adet")
            
            for table_schema, table_name in tables:
                # Debug: Tablo bilgilerini yazdır
                # print(f"  📋 Schema: '{table_schema}', Table: '{table_name}'")
                
                # Tablo adını formatla: public schema ise sadece tablo adı, değilse schema.tablo
                if table_schema == 'public':
                    full_table_name = table_name
                else:
                    full_table_name = f"{table_schema}.{table_name}"
                
                # print(f"     → Full table name: '{full_table_name}'")
                
                # Kolonları al
                cursor.execute("""
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                """, (table_schema, table_name))
                
                columns = []
                for col in cursor.fetchall():
                    columns.append({
                        "name": col[0],
                        "type": col[1],
                        "nullable": col[2] == "YES",
                        "default": col[3]
                    })
                
                schema["tables"].append({
                    "name": full_table_name,
                    "schema": table_schema,
                    "columns": columns
                })
            
            cursor.close()
        except Exception as e:
            print(f"Şema analizi hatası: {e}")
        
        return schema
    
    def execute_query(self, query: str, params: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """SQL sorgusu çalıştır ve sonuçları döndür"""
        if not self.connection:
            raise Exception("Veritabanı bağlantısı yok")
        
        # Güvenlik: Sadece SELECT sorgularına izin ver
        query_upper = query.strip().upper()
        dangerous_keywords = ['DROP', 'DELETE', 'TRUNCATE', 'ALTER', 'CREATE', 'INSERT', 'UPDATE', 'GRANT', 'REVOKE']
        if any(keyword in query_upper for keyword in dangerous_keywords):
            raise ValueError("Güvenlik: Sadece SELECT sorgularına izin verilir")
        
        try:
            cursor = self.connection.cursor(cursor_factory=RealDictCursor)
            cursor.execute(query, params)
            results = [dict(row) for row in cursor.fetchall()]
            cursor.close()
            return results
        except Exception as e:
            print(f"SQL sorgu hatası: {e}")
            raise
    
    def close(self):
        """Bağlantıyı kapat"""
        if self.connection:
            self.connection.close()
            self.connection = None