# backend/app/database_connectors/base.py

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

class BaseDatabaseConnector(ABC):
    """Harici veritabanlarına bağlanmak için base class"""
    
    @abstractmethod
    def connect(self, connection_string: str, **kwargs) -> bool:
        """Veritabanına bağlan"""
        pass
    
    @abstractmethod
    def get_schema(self) -> Dict[str, Any]:
        """Veritabanı şemasını getir (tablolar, kolonlar, ilişkiler)"""
        pass
    
    @abstractmethod
    def execute_query(self, query: str, params: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """SQL sorgusu çalıştır ve sonuçları döndür"""
        pass
    
    @abstractmethod
    def test_connection(self) -> bool:
        """Bağlantıyı test et"""
        pass
    
    @abstractmethod
    def close(self):
        """Bağlantıyı kapat"""
        pass

