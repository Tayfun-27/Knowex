# backend/app/storage_adapters/google_drive_adapter.py

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.auth.transport.requests import Request
from app.storage_adapters.external_storage_base import BaseExternalStorageAdapter
from typing import Dict, Any, Optional, List
from datetime import datetime
import io
import json

class GoogleDriveAdapter(BaseExternalStorageAdapter):
    """Google Drive API için adapter"""
    
    SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
    AUTH_URI = 'https://accounts.google.com/o/oauth2/auth'
    TOKEN_URI = 'https://oauth2.googleapis.com/token'
    
    def get_auth_url(self, client_id: str, client_secret: str, redirect_uri: str, state: Optional[str] = None) -> str:
        """OAuth 2.0 authorization URL'i oluşturur"""
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": self.AUTH_URI,
                    "token_uri": self.TOKEN_URI,
                    "redirect_uris": [redirect_uri]
                }
            },
            scopes=self.SCOPES
        )
        flow.redirect_uri = redirect_uri
        auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline', state=state)
        return auth_url
    
    def exchange_code_for_tokens(
        self, 
        code: str, 
        client_id: str, 
        client_secret: str, 
        redirect_uri: str
    ) -> Dict[str, Any]:
        """Authorization code'u token'lara çevirir"""
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": self.AUTH_URI,
                    "token_uri": self.TOKEN_URI,
                    "redirect_uris": [redirect_uri]
                }
            },
            scopes=self.SCOPES
        )
        flow.redirect_uri = redirect_uri
        flow.fetch_token(code=code)
        
        credentials = flow.credentials
        
        return {
            "access_token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "expires_in": int(credentials.expiry.timestamp() - datetime.now().timestamp()) if credentials.expiry else 3600,
            "token_type": "Bearer"
        }
    
    def refresh_access_token(
        self, 
        refresh_token: str, 
        client_id: str, 
        client_secret: str
    ) -> Dict[str, Any]:
        """Refresh token ile yeni access token alır"""
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=self.TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret
        )
        
        creds.refresh(Request())
        
        return {
            "access_token": creds.token,
            "expires_in": int(creds.expiry.timestamp() - datetime.now().timestamp()) if creds.expiry else 3600,
            "token_type": "Bearer"
        }
    
    def list_files(
        self, 
        folder_id: Optional[str] = None,
        access_token: str = None,
        page_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """Google Drive'dan dosyaları listeler"""
        creds = Credentials(token=access_token)
        service = build('drive', 'v3', credentials=creds)
        
        query = "trashed=false and mimeType != 'application/vnd.google-apps.folder'"
        if folder_id:
            query += f" and '{folder_id}' in parents"
        
        params = {
            'q': query,
            'fields': 'nextPageToken, files(id, name, mimeType, size, modifiedTime, webViewLink)',
            'pageSize': 100
        }
        
        if page_token:
            params['pageToken'] = page_token
        
        results = service.files().list(**params).execute()
        
        files = []
        for file in results.get('files', []):
            modified_time = None
            if file.get('modifiedTime'):
                try:
                    modified_time = datetime.fromisoformat(file['modifiedTime'].replace('Z', '+00:00'))
                except:
                    modified_time = datetime.now()
            
            files.append({
                "id": file['id'],
                "name": file['name'],
                "mime_type": file.get('mimeType', ''),
                "size": int(file.get('size', 0)),
                "modified_time": modified_time,
                "web_view_link": file.get('webViewLink', ''),
                "is_folder": False
            })
        
        return {
            "files": files,
            "next_page_token": results.get('nextPageToken')
        }
    
    def download_file(self, file_id: str, access_token: str, mime_type: Optional[str] = None) -> bytes:
        """
        Google Drive'dan dosyayı indirir.
        Google Workspace dosyaları (Docs, Sheets, Slides) için export API kullanır.
        """
        creds = Credentials(token=access_token)
        service = build('drive', 'v3', credentials=creds)
        
        # Eğer MIME type verilmemişse, dosya metadata'sını al
        if not mime_type:
            file_metadata = service.files().get(
                fileId=file_id,
                fields='mimeType'
            ).execute()
            mime_type = file_metadata.get('mimeType', '')
        
        # Google Workspace dosyaları için export kullan
        if mime_type.startswith('application/vnd.google-apps.'):
            export_mime_type = self._get_export_mime_type(mime_type)
            if not export_mime_type:
                raise Exception(f"Bu dosya tipi export edilemiyor: {mime_type}")
            
            request = service.files().export_media(fileId=file_id, mimeType=export_mime_type)
            file_content = io.BytesIO()
            downloader = MediaIoBaseDownload(file_content, request)
            
            done = False
            while not done:
                status, done = downloader.next_chunk()
            
            return file_content.getvalue()
        else:
            # Normal dosyalar için standart download
            request = service.files().get_media(fileId=file_id)
            file_content = io.BytesIO()
            downloader = MediaIoBaseDownload(file_content, request)
            
            done = False
            while not done:
                status, done = downloader.next_chunk()
            
            return file_content.getvalue()
    
    def _get_export_mime_type(self, google_mime_type: str) -> Optional[str]:
        """
        Google Workspace MIME type'ından export için uygun MIME type döndürür.
        """
        export_map = {
            # Google Docs -> DOCX (daha iyi format desteği için)
            'application/vnd.google-apps.document': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            # Google Sheets -> XLSX
            'application/vnd.google-apps.spreadsheet': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            # Google Slides -> PPTX
            'application/vnd.google-apps.presentation': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
            # Google Drawings -> PNG
            'application/vnd.google-apps.drawing': 'image/png',
            # Google Scripts -> JSON (genellikle export edilemez, ama deneyelim)
            'application/vnd.google-apps.script': 'application/vnd.google-apps.script+json',
        }
        
        return export_map.get(google_mime_type)
    
    def get_file_metadata(
        self,
        file_id: str,
        access_token: str
    ) -> Dict[str, Any]:
        """Dosya metadata'sını getirir"""
        creds = Credentials(token=access_token)
        service = build('drive', 'v3', credentials=creds)
        
        file = service.files().get(
            fileId=file_id,
            fields='id, name, mimeType, size, modifiedTime, webViewLink'
        ).execute()
        
        modified_time = None
        if file.get('modifiedTime'):
            try:
                modified_time = datetime.fromisoformat(file['modifiedTime'].replace('Z', '+00:00'))
            except:
                modified_time = datetime.now()
        
        return {
            "id": file['id'],
            "name": file['name'],
            "mime_type": file.get('mimeType', ''),
            "size": int(file.get('size', 0)),
            "modified_time": modified_time,
            "web_view_link": file.get('webViewLink', '')
        }
    
    def list_folders(
        self,
        parent_folder_id: Optional[str] = None,
        access_token: str = None,
        page_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """Google Drive'dan klasörleri listeler"""
        creds = Credentials(token=access_token)
        service = build('drive', 'v3', credentials=creds)
        
        query = "trashed=false and mimeType = 'application/vnd.google-apps.folder'"
        if parent_folder_id:
            query += f" and '{parent_folder_id}' in parents"
        else:
            # Root klasöründeki klasörleri getir
            query += " and 'root' in parents"
        
        params = {
            'q': query,
            'fields': 'nextPageToken, files(id, name, mimeType, modifiedTime)',
            'pageSize': 100,
            'orderBy': 'name'
        }
        
        if page_token:
            params['pageToken'] = page_token
        
        results = service.files().list(**params).execute()
        
        folders = []
        for folder in results.get('files', []):
            modified_time = None
            if folder.get('modifiedTime'):
                try:
                    modified_time = datetime.fromisoformat(folder['modifiedTime'].replace('Z', '+00:00'))
                except:
                    modified_time = datetime.now()
            
            folders.append({
                "id": folder['id'],
                "name": folder['name'],
                "mime_type": folder.get('mimeType', ''),
                "modified_time": modified_time,
                "is_folder": True
            })
        
        return {
            "folders": folders,
            "next_page_token": results.get('nextPageToken')
        }

