# backend/app/storage_adapters/external_storage_base.py

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from datetime import datetime

class BaseExternalStorageAdapter(ABC):
    """
    Harici depolama servisleri (Google Drive, OneDrive) için temel sınıf.
    Bu sınıf, farklı cloud storage servislerine bağlanmak için ortak bir arayüz sağlar.
    """
    
    @abstractmethod
    def get_auth_url(self, client_id: str, client_secret: str, redirect_uri: str) -> str:
        """
        OAuth 2.0 authorization URL'i oluşturur.
        
        Args:
            client_id: OAuth client ID
            client_secret: OAuth client secret
            redirect_uri: OAuth callback URL'i
            
        Returns:
            Authorization URL (kullanıcı bu URL'e yönlendirilecek)
        """
        pass
    
    @abstractmethod
    def exchange_code_for_tokens(
        self, 
        code: str, 
        client_id: str, 
        client_secret: str, 
        redirect_uri: str
    ) -> Dict[str, Any]:
        """
        OAuth authorization code'u access token ve refresh token'a çevirir.
        
        Args:
            code: OAuth callback'ten gelen authorization code
            client_id: OAuth client ID
            client_secret: OAuth client secret
            redirect_uri: OAuth callback URL'i
            
        Returns:
            {
                "access_token": "...",
                "refresh_token": "...",
                "expires_in": 3600,
                "token_type": "Bearer"
            }
        """
        pass
    
    @abstractmethod
    def refresh_access_token(
        self, 
        refresh_token: str, 
        client_id: str, 
        client_secret: str
    ) -> Dict[str, Any]:
        """
        Refresh token kullanarak yeni access token alır.
        
        Args:
            refresh_token: Daha önce alınan refresh token
            client_id: OAuth client ID
            client_secret: OAuth client secret
            
        Returns:
            {
                "access_token": "...",
                "refresh_token": "...",  # Yeni refresh token (bazı servislerde)
                "expires_in": 3600
            }
        """
        pass
    
    @abstractmethod
    def list_files(
        self, 
        folder_id: Optional[str] = None,
        access_token: str = None,
        page_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Dosyaları listeler.
        
        Args:
            folder_id: Belirli bir klasörün ID'si (None ise root klasör)
            access_token: OAuth access token
            page_token: Pagination için token (bazı servislerde)
            
        Returns:
            {
                "files": [
                    {
                        "id": "file_id",
                        "name": "file.pdf",
                        "mime_type": "application/pdf",
                        "size": 12345,
                        "modified_time": datetime,
                        "web_view_link": "https://...",
                        "is_folder": False
                    }
                ],
                "next_page_token": "..."  # Varsa
            }
        """
        pass
    
    @abstractmethod
    def download_file(
        self, 
        file_id: str, 
        access_token: str
    ) -> bytes:
        """
        Dosyayı indirir ve bytes olarak döndürür.
        
        Args:
            file_id: İndirilecek dosyanın ID'si
            access_token: OAuth access token
            
        Returns:
            Dosya içeriği (bytes)
        """
        pass
    
    @abstractmethod
    def get_file_metadata(
        self,
        file_id: str,
        access_token: str
    ) -> Dict[str, Any]:
        """
        Dosya metadata'sını getirir.
        
        Args:
            file_id: Dosyanın ID'si
            access_token: OAuth access token
            
        Returns:
            {
                "id": "file_id",
                "name": "file.pdf",
                "mime_type": "application/pdf",
                "size": 12345,
                "modified_time": datetime,
                "web_view_link": "https://..."
            }
        """
        pass

