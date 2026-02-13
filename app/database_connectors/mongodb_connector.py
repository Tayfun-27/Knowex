# backend/app/database_connectors/mongodb_connector.py

from pymongo import MongoClient
from typing import List, Dict, Any, Optional
from app.database_connectors.base import BaseDatabaseConnector

class MongoDBConnector(BaseDatabaseConnector):
    def __init__(self):
        self.client = None
        self.db = None
        self.connection_string = None
    
    def connect(self, connection_string: str, **kwargs) -> bool:
        try:
            self.connection_string = connection_string
            self.client = MongoClient(connection_string)
            # Database adını connection string'den çıkar veya varsayılan kullan
            db_name = kwargs.get('database_name', 'admin')
            self.db = self.client[db_name]
            # Bağlantıyı test et
            self.client.admin.command('ping')
            return True
        except Exception as e:
            print(f"MongoDB bağlantı hatası: {e}")
            return False
    
    def test_connection(self) -> bool:
        if not self.client:
            return False
        try:
            self.client.admin.command('ping')
            return True
        except Exception as e:
            print(f"MongoDB bağlantı testi hatası: {e}")
            return False
    
    def get_schema(self) -> Dict[str, Any]:
        """MongoDB şemasını analiz et"""
        if not self.db:
            return {}
        
        schema = {
            "collections": [],
            "database_name": self.db.name
        }
        
        try:
            collections = self.db.list_collection_names()
            
            for collection_name in collections:
                collection = self.db[collection_name]
                # İlk birkaç dokümandan örnek al
                sample_docs = list(collection.find().limit(5))
                
                # Kolonları çıkar (örnek dokümanlardan)
                columns = set()
                for doc in sample_docs:
                    columns.update(doc.keys())
                
                schema["collections"].append({
                    "name": collection_name,
                    "sample_fields": list(columns)
                })
        except Exception as e:
            print(f"Şema analizi hatası: {e}")
        
        return schema
    
    def execute_query(self, query: str, params: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """MongoDB sorgusu çalıştır (find query)"""
        if not self.db:
            raise Exception("Veritabanı bağlantısı yok")
        
        # MongoDB için query string'i parse et (basit bir yaklaşım)
        # Gerçek uygulamada daha gelişmiş bir parser kullanılabilir
        try:
            import json
            # Query string'i JSON'a çevirmeye çalış
            if params and 'collection' in params:
                collection_name = params['collection']
                collection = self.db[collection_name]
                
                # Basit find sorgusu
                find_params = params.get('find', {})
                results = list(collection.find(find_params).limit(100))
                
                # ObjectId'leri string'e çevir
                for doc in results:
                    if '_id' in doc:
                        doc['_id'] = str(doc['_id'])
                
                return results
            else:
                raise ValueError("MongoDB sorgusu için collection parametresi gerekli")
        except Exception as e:
            print(f"MongoDB sorgu hatası: {e}")
            raise
    
    def close(self):
        """Bağlantıyı kapat"""
        if self.client:
            self.client.close()
            self.client = None
            self.db = None

