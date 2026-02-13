# backend/app/storage_adapters/onedrive_adapter.py

from app.storage_adapters.external_storage_base import BaseExternalStorageAdapter
from typing import Dict, Any, Optional
from datetime import datetime
import requests
import msal

class OneDriveAdapter(BaseExternalStorageAdapter):
    """Microsoft OneDrive API için adapter"""
    
    AUTHORITY = "https://login.microsoftonline.com/common"
    SCOPES = ["Files.Read", "offline_access"]
    API_BASE = "https://graph.microsoft.com/v1.0"
    
    def get_auth_url(self, client_id: str, client_secret: str, redirect_uri: str, state: Optional[str] = None) -> str:
        """OAuth 2.0 authorization URL'i oluşturur"""
        app = msal.PublicClientApplication(
            client_id=client_id,
            authority=self.AUTHORITY
        )
        
        auth_url = app.get_authorization_request_url(
            scopes=self.SCOPES,
            redirect_uri=redirect_uri,
            state=state
        )
        
        return auth_url
    
    def exchange_code_for_tokens(
        self, 
        code: str, 
        client_id: str, 
        client_secret: str, 
        redirect_uri: str
    ) -> Dict[str, Any]:
        """Authorization code'u token'lara çevirir"""
        app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=self.AUTHORITY
        )
        
        result = app.acquire_token_by_authorization_code(
            code=code,
            scopes=self.SCOPES,
            redirect_uri=redirect_uri
        )
        
        if "error" in result:
            raise Exception(f"Token alınamadı: {result.get('error_description', result.get('error'))}")
        
        expires_in = result.get('expires_in', 3600)
        if 'expires_on' in result:
            expires_in = int(result['expires_on'] - datetime.now().timestamp())
        
        return {
            "access_token": result['access_token'],
            "refresh_token": result.get('refresh_token', ''),
            "expires_in": expires_in,
            "token_type": result.get('token_type', 'Bearer')
        }
    
    def refresh_access_token(
        self, 
        refresh_token: str, 
        client_id: str, 
        client_secret: str
    ) -> Dict[str, Any]:
        """Refresh token ile yeni access token alır"""
        app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=self.AUTHORITY
        )
        
        result = app.acquire_token_by_refresh_token(
            refresh_token=refresh_token,
            scopes=self.SCOPES
        )
        
        if "error" in result:
            raise Exception(f"Token yenilenemedi: {result.get('error_description', result.get('error'))}")
        
        expires_in = result.get('expires_in', 3600)
        if 'expires_on' in result:
            expires_in = int(result['expires_on'] - datetime.now().timestamp())
        
        return {
            "access_token": result['access_token'],
            "refresh_token": result.get('refresh_token', refresh_token),  # Yeni refresh token varsa onu kullan
            "expires_in": expires_in,
            "token_type": result.get('token_type', 'Bearer')
        }
    
    def list_files(
        self, 
        folder_id: Optional[str] = None,
        access_token: str = None,
        page_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """OneDrive'dan dosyaları listeler"""
        if folder_id:
            url = f"{self.API_BASE}/me/drive/items/{folder_id}/children"
        else:
            url = f"{self.API_BASE}/me/drive/root/children"
        
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {
            "$filter": "file ne null",  # Sadece dosyalar, klasörler değil
            "$top": 100
        }
        
        if page_token:
            params["$skiptoken"] = page_token
        
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        
        data = response.json()
        files = []
        
        for item in data.get('value', []):
            if 'file' in item:  # Klasör değil, dosya
                modified_time = None
                if item.get('lastModifiedDateTime'):
                    try:
                        modified_time = datetime.fromisoformat(item['lastModifiedDateTime'].replace('Z', '+00:00'))
                    except:
                        modified_time = datetime.now()
                
                files.append({
                    "id": item['id'],
                    "name": item['name'],
                    "mime_type": item.get('file', {}).get('mimeType', ''),
                    "size": item.get('size', 0),
                    "modified_time": modified_time,
                    "web_view_link": item.get('webUrl', ''),
                    "is_folder": False
                })
        
        next_page_token = None
        if '@odata.nextLink' in data:
            # Next link'ten skip token'ı çıkar
            next_link = data['@odata.nextLink']
            if '$skiptoken=' in next_link:
                next_page_token = next_link.split('$skiptoken=')[1].split('&')[0]
        
        return {
            "files": files,
            "next_page_token": next_page_token
        }
    
    def download_file(self, file_id: str, access_token: str) -> bytes:
        """OneDrive'dan dosyayı indirir"""
        url = f"{self.API_BASE}/me/drive/items/{file_id}/content"
        headers = {"Authorization": f"Bearer {access_token}"}
        
        response = requests.get(url, headers=headers, stream=True)
        response.raise_for_status()
        
        return response.content
    
    def get_file_metadata(
        self,
        file_id: str,
        access_token: str
    ) -> Dict[str, Any]:
        """Dosya metadata'sını getirir"""
        url = f"{self.API_BASE}/me/drive/items/{file_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        item = response.json()
        
        modified_time = None
        if item.get('lastModifiedDateTime'):
            try:
                modified_time = datetime.fromisoformat(item['lastModifiedDateTime'].replace('Z', '+00:00'))
            except:
                modified_time = datetime.now()
        
        return {
            "id": item['id'],
            "name": item['name'],
            "mime_type": item.get('file', {}).get('mimeType', '') if 'file' in item else '',
            "size": item.get('size', 0),
            "modified_time": modified_time,
            "web_view_link": item.get('webUrl', '')
        }

