# backend/app/api/v1/user_storage.py

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from typing import Optional
from datetime import datetime
from app.dependencies import get_current_user, get_db_repository
from app.schemas.user import UserInDB
from app.repositories.base import BaseRepository
from google.cloud import firestore
import os
import base64
import json

router = APIRouter()

def safe_error_message(e: Exception, default_message: str) -> str:
    """Production'da hassas bilgi sızıntısını önlemek için güvenli hata mesajı döndürür."""
    from app.core.config import ENVIRONMENT, DEBUG
    if ENVIRONMENT == "production" and not DEBUG:
        return default_message
    else:
        return f"{default_message}: {str(e)}"

def create_error_html(error_message: str, status_code: int = 400) -> HTMLResponse:
    """OAuth callback hataları için HTML sayfası oluşturur."""
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>OAuth Hatası</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
                background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
                color: white;
            }}
            .container {{
                text-align: center;
                padding: 40px;
                background: rgba(255, 255, 255, 0.1);
                border-radius: 20px;
                backdrop-filter: blur(10px);
                box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.37);
            }}
            .error-icon {{
                font-size: 64px;
                margin-bottom: 20px;
            }}
            h1 {{
                margin: 0 0 10px 0;
                font-size: 24px;
            }}
            p {{
                margin: 0;
                opacity: 0.9;
                font-size: 16px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="error-icon">❌</div>
            <h1>Bağlantı Hatası</h1>
            <p>{error_message}</p>
            <p style="margin-top: 10px; font-size: 14px; opacity: 0.7;">Bu pencereyi kapatabilirsiniz.</p>
        </div>
        <script>
            // Parent window'a hata mesajı gönder
            if (window.opener) {{
                window.opener.postMessage({{
                    type: 'oauth-error',
                    error: {json.dumps(error_message)}
                }}, '*');
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content, status_code=status_code)

@router.get("/external-storage")
def get_user_external_storage(current_user: UserInDB = Depends(get_current_user)):
    """Kullanıcının kendi harici depolama bağlantılarını getirir"""
    db = firestore.Client()
    doc = db.collection("user_external_storage").document(current_user.id).get()
    if doc.exists:
        data = doc.to_dict()
        # Hassas bilgileri gizle
        if 'access_token' in data:
            data['access_token'] = '••••••••' if data.get('access_token') else None
        if 'refresh_token' in data:
            data['refresh_token'] = '••••••••' if data.get('refresh_token') else None
        return data
    return {"storage_type": None, "is_enabled": False}

@router.get("/external-storage/auth-url")
def get_user_auth_url(
    storage_type: str = Query(..., description="Storage tipi: 'google_drive' veya 'onedrive'"),
    current_user: UserInDB = Depends(get_current_user),
    request: Request = None
):
    """Kullanıcı için OAuth 2.0 authorization URL'i döndürür"""
    from app.storage_adapters.google_drive_adapter import GoogleDriveAdapter
    from app.storage_adapters.onedrive_adapter import OneDriveAdapter
    from app.core.config import (
        GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET,
        ONEDRIVE_CLIENT_ID, ONEDRIVE_CLIENT_SECRET
    )
    
    # Base URL'i request'ten al veya environment variable'dan, yoksa request'ten oluştur
    base_url = os.environ.get("API_BASE_URL")
    if not base_url and request:
        # Request'ten base URL'i oluştur
        base_url = str(request.base_url).rstrip('/')
    elif not base_url:
        # Fallback: localhost (sadece development için)
        base_url = "http://localhost:8000"
    
    redirect_uri = f"{base_url}/api/v1/user/external-storage/oauth-callback"
    
    # State'e user_id ve storage_type ekle (güvenlik için)
    state_data = {
        "user_id": current_user.id,
        "storage_type": storage_type,
        "tenant_id": current_user.tenant_id
    }
    state = base64.urlsafe_b64encode(json.dumps(state_data).encode()).decode()
    
    try:
        if storage_type == "google_drive":
            if not GOOGLE_DRIVE_CLIENT_ID or not GOOGLE_DRIVE_CLIENT_SECRET:
                raise HTTPException(
                    status_code=500,
                    detail="Google Drive OAuth yapılandırması eksik. Lütfen sistem yöneticisi ile iletişime geçin."
                )
            
            adapter = GoogleDriveAdapter()
            auth_url = adapter.get_auth_url(
                client_id=GOOGLE_DRIVE_CLIENT_ID,
                client_secret=GOOGLE_DRIVE_CLIENT_SECRET,
                redirect_uri=redirect_uri,
                state=state
            )
            return {"auth_url": auth_url, "storage_type": storage_type}
        
        elif storage_type == "onedrive":
            if not ONEDRIVE_CLIENT_ID or not ONEDRIVE_CLIENT_SECRET:
                raise HTTPException(
                    status_code=500,
                    detail="OneDrive OAuth yapılandırması eksik. Lütfen sistem yöneticisi ile iletişime geçin."
                )
            
            adapter = OneDriveAdapter()
            auth_url = adapter.get_auth_url(
                client_id=ONEDRIVE_CLIENT_ID,
                client_secret=ONEDRIVE_CLIENT_SECRET,
                redirect_uri=redirect_uri,
                state=state
            )
            return {"auth_url": auth_url, "storage_type": storage_type}
        
        else:
            raise HTTPException(status_code=400, detail=f"Desteklenmeyen storage tipi: {storage_type}")
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=safe_error_message(e, "OAuth URL oluşturulurken bir hata oluştu")
        )

@router.get("/external-storage/oauth-callback")
def user_oauth_callback(
    code: str = Query(..., description="OAuth authorization code"),
    state: Optional[str] = Query(None, description="OAuth state parameter"),
    error: Optional[str] = Query(None, description="OAuth error"),
    request: Request = None
):
    """Kullanıcı OAuth callback endpoint'i - authorization code'u token'lara çevirir
    
    Not: Bu endpoint public'tir çünkü OAuth redirect sırasında kullanıcı henüz authenticate olmamış olabilir.
    Güvenlik state parametresi ile sağlanır.
    """
    from app.storage_adapters.google_drive_adapter import GoogleDriveAdapter
    from app.storage_adapters.onedrive_adapter import OneDriveAdapter
    
    if error:
        # Hata durumu için HTML sayfası döndür
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>OAuth Hatası</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                    background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
                    color: white;
                }}
                .container {{
                    text-align: center;
                    padding: 40px;
                    background: rgba(255, 255, 255, 0.1);
                    border-radius: 20px;
                    backdrop-filter: blur(10px);
                    box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.37);
                }}
                .error-icon {{
                    font-size: 64px;
                    margin-bottom: 20px;
                }}
                h1 {{
                    margin: 0 0 10px 0;
                    font-size: 24px;
                }}
                p {{
                    margin: 0;
                    opacity: 0.9;
                    font-size: 16px;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="error-icon">❌</div>
                <h1>Bağlantı Hatası</h1>
                <p>OAuth hatası: {error}</p>
                <p style="margin-top: 10px; font-size: 14px; opacity: 0.7;">Bu pencereyi kapatabilirsiniz.</p>
            </div>
            <script>
                // Parent window'a hata mesajı gönder
                if (window.opener) {{
                    window.opener.postMessage({{
                        type: 'oauth-error',
                        error: '{error}'
                    }}, '*');
                }}
            </script>
        </body>
        </html>
        """
        return HTMLResponse(content=html_content, status_code=400)
    
    if not code:
        html_content = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>OAuth Hatası</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                    background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
                    color: white;
                }
                .container {
                    text-align: center;
                    padding: 40px;
                    background: rgba(255, 255, 255, 0.1);
                    border-radius: 20px;
                    backdrop-filter: blur(10px);
                    box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.37);
                }
                .error-icon {
                    font-size: 64px;
                    margin-bottom: 20px;
                }
                h1 {
                    margin: 0 0 10px 0;
                    font-size: 24px;
                }
                p {
                    margin: 0;
                    opacity: 0.9;
                    font-size: 16px;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="error-icon">❌</div>
                <h1>Bağlantı Hatası</h1>
                <p>Authorization code bulunamadı.</p>
                <p style="margin-top: 10px; font-size: 14px; opacity: 0.7;">Bu pencereyi kapatabilirsiniz.</p>
            </div>
            <script>
                if (window.opener) {
                    window.opener.postMessage({
                        type: 'oauth-error',
                        error: 'Authorization code bulunamadı'
                    }, '*');
                }
            </script>
        </body>
        </html>
        """
        return HTMLResponse(content=html_content, status_code=400)
    
    # State'ten bilgileri al
    if not state:
        html_content = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>OAuth Hatası</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                    background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
                    color: white;
                }
                .container {
                    text-align: center;
                    padding: 40px;
                    background: rgba(255, 255, 255, 0.1);
                    border-radius: 20px;
                    backdrop-filter: blur(10px);
                    box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.37);
                }
                .error-icon {
                    font-size: 64px;
                    margin-bottom: 20px;
                }
                h1 {
                    margin: 0 0 10px 0;
                    font-size: 24px;
                }
                p {
                    margin: 0;
                    opacity: 0.9;
                    font-size: 16px;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="error-icon">❌</div>
                <h1>Bağlantı Hatası</h1>
                <p>State parametresi bulunamadı.</p>
                <p style="margin-top: 10px; font-size: 14px; opacity: 0.7;">Bu pencereyi kapatabilirsiniz.</p>
            </div>
            <script>
                if (window.opener) {
                    window.opener.postMessage({
                        type: 'oauth-error',
                        error: 'State parametresi bulunamadı'
                    }, '*');
                }
            </script>
        </body>
        </html>
        """
        return HTMLResponse(content=html_content, status_code=400)
    
    try:
        state_data = json.loads(base64.urlsafe_b64decode(state.encode()).decode())
        user_id = state_data.get('user_id')
        storage_type = state_data.get('storage_type')
        tenant_id = state_data.get('tenant_id')
    except Exception as e:
        return create_error_html(f"Geçersiz state parametresi: {str(e)}", 400)
    
    # State'ten gelen user_id ile kullanıcıyı doğrula
    if not user_id:
        return create_error_html("State'te user_id bulunamadı", 400)
    
    # Kullanıcıyı Firestore'dan direkt al (UserInDB formatında)
    try:
        firestore_db = firestore.Client()
        user_doc = firestore_db.collection("users").document(user_id).get()
        if not user_doc.exists:
            return create_error_html("Kullanıcı bulunamadı", 404)
        
        user_data = user_doc.to_dict() or {}
        user_data["id"] = user_doc.id
        # Geriye dönük uyumluluk: roles yoksa role'den türet
        if "roles" not in user_data or not user_data.get("roles"):
            user_data["roles"] = [user_data.get("role", "User")]
        user = UserInDB(**user_data)
        
        # Tenant kontrolü
        if tenant_id and user.tenant_id != tenant_id:
            return create_error_html("Yetkisiz erişim: Tenant eşleşmiyor", 403)
    except Exception as e:
        return create_error_html(f"Kullanıcı doğrulama hatası: {str(e)}", 500)
    
    # Config'den Client ID/Secret al
    from app.core.config import (
        GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET,
        ONEDRIVE_CLIENT_ID, ONEDRIVE_CLIENT_SECRET
    )
    
    # Base URL'i request'ten al veya environment variable'dan, yoksa request'ten oluştur
    base_url = os.environ.get("API_BASE_URL")
    if not base_url and request:
        # Request'ten base URL'i oluştur
        base_url = str(request.base_url).rstrip('/')
    elif not base_url:
        # Fallback: localhost (sadece development için)
        base_url = "http://localhost:8000"
    
    redirect_uri = f"{base_url}/api/v1/user/external-storage/oauth-callback"
    
    try:
        if storage_type == "google_drive":
            if not GOOGLE_DRIVE_CLIENT_ID or not GOOGLE_DRIVE_CLIENT_SECRET:
                return create_error_html("Google Drive OAuth yapılandırması eksik", 500)
            
            adapter = GoogleDriveAdapter()
            tokens = adapter.exchange_code_for_tokens(
                code=code,
                client_id=GOOGLE_DRIVE_CLIENT_ID,
                client_secret=GOOGLE_DRIVE_CLIENT_SECRET,
                redirect_uri=redirect_uri
            )
            
            # Kullanıcının token'larını kaydet
            firestore_db = firestore.Client()
            firestore_db.collection("user_external_storage").document(user_id).set({
                "tenant_id": tenant_id,
                "storage_type": "google_drive",
                "access_token": tokens['access_token'],
                "refresh_token": tokens.get('refresh_token', ''),
                "is_enabled": True,
                "connected_at": datetime.now(),
                "updated_at": datetime.now()
            }, merge=True)
            
            # HTML sayfası döndür - parent window'a mesaj gönder ve pencereyi kapat
            html_content = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>Google Drive Bağlantısı Başarılı</title>
                <style>
                    body {
                        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                        display: flex;
                        justify-content: center;
                        align-items: center;
                        height: 100vh;
                        margin: 0;
                        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                        color: white;
                    }
                    .container {
                        text-align: center;
                        padding: 40px;
                        background: rgba(255, 255, 255, 0.1);
                        border-radius: 20px;
                        backdrop-filter: blur(10px);
                        box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.37);
                    }
                    .success-icon {
                        font-size: 64px;
                        margin-bottom: 20px;
                    }
                    h1 {
                        margin: 0 0 10px 0;
                        font-size: 24px;
                    }
                    p {
                        margin: 0;
                        opacity: 0.9;
                        font-size: 16px;
                    }
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="success-icon">✅</div>
                    <h1>Bağlantı Başarılı!</h1>
                    <p>Google Drive bağlantısı başarıyla kuruldu.</p>
                    <p style="margin-top: 10px; font-size: 14px; opacity: 0.7;">Bu pencere otomatik olarak kapanacak...</p>
                </div>
                <script>
                    // Parent window'a mesaj gönder
                    if (window.opener) {
                        window.opener.postMessage({
                            type: 'oauth-success',
                            storage_type: 'google_drive',
                            message: 'Google Drive bağlantısı başarıyla kuruldu.'
                        }, '*');
                    }
                    // 2 saniye sonra pencereyi kapat
                    setTimeout(function() {
                        window.close();
                    }, 2000);
                </script>
            </body>
            </html>
            """
            return HTMLResponse(content=html_content)
        
        elif storage_type == "onedrive":
            if not ONEDRIVE_CLIENT_ID or not ONEDRIVE_CLIENT_SECRET:
                return create_error_html("OneDrive OAuth yapılandırması eksik", 500)
            
            adapter = OneDriveAdapter()
            tokens = adapter.exchange_code_for_tokens(
                code=code,
                client_id=ONEDRIVE_CLIENT_ID,
                client_secret=ONEDRIVE_CLIENT_SECRET,
                redirect_uri=redirect_uri
            )
            
            # Kullanıcının token'larını kaydet
            firestore_db = firestore.Client()
            firestore_db.collection("user_external_storage").document(user_id).set({
                "tenant_id": tenant_id,
                "storage_type": "onedrive",
                "access_token": tokens['access_token'],
                "refresh_token": tokens.get('refresh_token', ''),
                "is_enabled": True,
                "connected_at": datetime.now(),
                "updated_at": datetime.now()
            }, merge=True)
            
            # HTML sayfası döndür - parent window'a mesaj gönder ve pencereyi kapat
            html_content = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>OneDrive Bağlantısı Başarılı</title>
                <style>
                    body {
                        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                        display: flex;
                        justify-content: center;
                        align-items: center;
                        height: 100vh;
                        margin: 0;
                        background: linear-gradient(135deg, #0078d4 0%, #106ebe 100%);
                        color: white;
                    }
                    .container {
                        text-align: center;
                        padding: 40px;
                        background: rgba(255, 255, 255, 0.1);
                        border-radius: 20px;
                        backdrop-filter: blur(10px);
                        box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.37);
                    }
                    .success-icon {
                        font-size: 64px;
                        margin-bottom: 20px;
                    }
                    h1 {
                        margin: 0 0 10px 0;
                        font-size: 24px;
                    }
                    p {
                        margin: 0;
                        opacity: 0.9;
                        font-size: 16px;
                    }
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="success-icon">✅</div>
                    <h1>Bağlantı Başarılı!</h1>
                    <p>OneDrive bağlantısı başarıyla kuruldu.</p>
                    <p style="margin-top: 10px; font-size: 14px; opacity: 0.7;">Bu pencere otomatik olarak kapanacak...</p>
                </div>
                <script>
                    // Parent window'a mesaj gönder
                    if (window.opener) {
                        window.opener.postMessage({
                            type: 'oauth-success',
                            storage_type: 'onedrive',
                            message: 'OneDrive bağlantısı başarıyla kuruldu.'
                        }, '*');
                    }
                    // 2 saniye sonra pencereyi kapat
                    setTimeout(function() {
                        window.close();
                    }, 2000);
                </script>
            </body>
            </html>
            """
            return HTMLResponse(content=html_content)
        
        else:
            return create_error_html(f"Desteklenmeyen storage tipi: {storage_type}", 400)
    
    except Exception as e:
        print(f"OAuth callback hatası: {e}")
        error_msg = safe_error_message(e, f"{storage_type if 'storage_type' in locals() else 'Harici depolama'} bağlantısı kurulurken bir hata oluştu")
        return create_error_html(error_msg, 500)

@router.post("/external-storage/sync")
async def trigger_user_storage_sync(current_user: UserInDB = Depends(get_current_user)):
    """Kullanıcının kendi harici depolama dosyalarını senkronize eder"""
    from app.services.user_storage_sync import sync_user_external_storage
    from app.dependencies import get_db_repository
    from app.core.config import (
        GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET,
        ONEDRIVE_CLIENT_ID, ONEDRIVE_CLIENT_SECRET
    )
    
    db = get_db_repository()
    firestore_db = firestore.Client()
    
    # Kullanıcının bağlantı bilgilerini al
    user_storage_doc = firestore_db.collection("user_external_storage").document(current_user.id).get()
    if not user_storage_doc.exists:
        raise HTTPException(status_code=400, detail="Harici depolama bağlantısı bulunamadı. Lütfen önce bağlantı kurun.")
    
    user_storage = user_storage_doc.to_dict()
    if not user_storage.get('is_enabled') or not user_storage.get('storage_type'):
        raise HTTPException(status_code=400, detail="Harici depolama bağlantısı aktif değil")
    
    # Config'den Client ID/Secret al
    admin_settings = {
        'google_drive_client_id': GOOGLE_DRIVE_CLIENT_ID,
        'google_drive_client_secret': GOOGLE_DRIVE_CLIENT_SECRET,
        'onedrive_client_id': ONEDRIVE_CLIENT_ID,
        'onedrive_client_secret': ONEDRIVE_CLIENT_SECRET
    }
    
    try:
        result = await sync_user_external_storage(
            user=current_user,
            user_storage=user_storage,
            admin_settings=admin_settings,
            db=db
        )
        
        # Son senkronizasyon zamanını güncelle
        firestore_db.collection("user_external_storage").document(current_user.id).update({
            "last_sync_at": datetime.now()
        })
        
        # Detaylı mesaj oluştur
        storage_name = "Google Drive" if user_storage['storage_type'] == 'google_drive' else "OneDrive"
        message_parts = [f"{storage_name} senkronizasyonu tamamlandı."]
        
        if result.get('synced', 0) > 0:
            message_parts.append(f"{result['synced']} yeni dosya eklendi.")
        if result.get('updated', 0) > 0:
            message_parts.append(f"{result['updated']} dosya güncellendi.")
        if result.get('skipped', 0) > 0:
            message_parts.append(f"{result['skipped']} dosya atlandı (değişmemiş).")
        if result.get('errors', 0) > 0:
            message_parts.append(f"{result['errors']} dosyada hata oluştu.")
        
        return {
            "success": True,
            "message": " ".join(message_parts),
            "result": result
        }
    
    except Exception as e:
        print(f"Senkronizasyon hatası: {e}")
        raise HTTPException(
            status_code=500,
            detail=safe_error_message(e, "Senkronizasyon sırasında bir hata oluştu")
        )

@router.delete("/external-storage")
def disconnect_user_storage(current_user: UserInDB = Depends(get_current_user)):
    """Kullanıcının harici depolama bağlantısını keser"""
    db = firestore.Client()
    doc = db.collection("user_external_storage").document(current_user.id).get()
    
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Bağlantı bulunamadı")
    
    # Bağlantıyı devre dışı bırak (silme yerine)
    db.collection("user_external_storage").document(current_user.id).update({
        "is_enabled": False,
        "disconnected_at": datetime.now()
    })
    
    return {
        "success": True,
        "message": "Harici depolama bağlantısı kesildi."
    }

@router.get("/external-storage/folders")
def list_user_storage_folders(
    parent_id: Optional[str] = Query(None, description="Parent klasör ID'si (None ise root)"),
    current_user: UserInDB = Depends(get_current_user)
):
    """Kullanıcının harici depolama klasörlerini listeler"""
    from app.storage_adapters.google_drive_adapter import GoogleDriveAdapter
    from app.storage_adapters.onedrive_adapter import OneDriveAdapter
    
    db = firestore.Client()
    user_storage_doc = db.collection("user_external_storage").document(current_user.id).get()
    if not user_storage_doc.exists:
        raise HTTPException(status_code=400, detail="Harici depolama bağlantısı bulunamadı")
    
    user_storage = user_storage_doc.to_dict()
    storage_type = user_storage.get('storage_type')
    access_token = user_storage.get('access_token')
    
    if not storage_type or not access_token:
        raise HTTPException(status_code=400, detail="Harici depolama bağlantısı aktif değil")
    
    try:
        if storage_type == "google_drive":
            adapter = GoogleDriveAdapter()
            result = adapter.list_folders(
                parent_folder_id=parent_id,
                access_token=access_token
            )
            return {
                "success": True,
                "folders": result.get('folders', []),
                "storage_type": "google_drive"
            }
        
        elif storage_type == "onedrive":
            # OneDrive için klasör listeleme henüz implement edilmedi
            raise HTTPException(status_code=501, detail="OneDrive klasör listeleme henüz desteklenmiyor")
        
        else:
            raise HTTPException(status_code=400, detail=f"Desteklenmeyen storage tipi: {storage_type}")
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Klasör listeleme hatası: {e}")
        raise HTTPException(
            status_code=500,
            detail=safe_error_message(e, "Klasörler listelenirken bir hata oluştu")
        )

@router.put("/external-storage/folder")
def set_user_storage_folder(
    folder_id: Optional[str] = Query(None, description="Klasör ID'si (None ise tüm Drive)"),
    folder_name: Optional[str] = Query(None, description="Klasör adı (görüntüleme için)"),
    current_user: UserInDB = Depends(get_current_user)
):
    """Kullanıcının senkronize edilecek klasörünü ayarlar"""
    firestore_db = firestore.Client()
    user_storage_doc = firestore_db.collection("user_external_storage").document(current_user.id).get()
    if not user_storage_doc.exists:
        raise HTTPException(status_code=400, detail="Harici depolama bağlantısı bulunamadı")
    
    # Klasör bilgilerini güncelle
    update_data = {
        "folder_id": folder_id,  # None ise tüm Drive
        "folder_name": folder_name or "Tüm Drive",
        "updated_at": datetime.now()
    }
    
    firestore_db.collection("user_external_storage").document(current_user.id).update(update_data)
    
    return {
        "success": True,
        "message": f"Senkronizasyon klasörü ayarlandı: {folder_name or 'Tüm Drive'}",
        "folder_id": folder_id,
        "folder_name": folder_name or "Tüm Drive"
    }

@router.get("/external-storage/test")
def test_user_storage_connection(current_user: UserInDB = Depends(get_current_user)):
    """Kullanıcının harici depolama bağlantısını test eder"""
    from app.storage_adapters.google_drive_adapter import GoogleDriveAdapter
    from app.storage_adapters.onedrive_adapter import OneDriveAdapter
    
    db = firestore.Client()
    user_storage_doc = db.collection("user_external_storage").document(current_user.id).get()
    if not user_storage_doc.exists:
        raise HTTPException(status_code=400, detail="Harici depolama bağlantısı bulunamadı")
    
    user_storage = user_storage_doc.to_dict()
    storage_type = user_storage.get('storage_type')
    access_token = user_storage.get('access_token')
    
    if not storage_type or not access_token:
        raise HTTPException(status_code=400, detail="Harici depolama bağlantısı aktif değil")
    
    try:
        if storage_type == "google_drive":
            adapter = GoogleDriveAdapter()
            result = adapter.list_files(access_token=access_token)
            file_count = len(result.get('files', []))
            
            return {
                "success": True,
                "message": f"Google Drive bağlantısı başarılı. {file_count} dosya bulundu.",
                "storage_type": "google_drive",
                "file_count": file_count
            }
        
        elif storage_type == "onedrive":
            adapter = OneDriveAdapter()
            result = adapter.list_files(access_token=access_token)
            file_count = len(result.get('files', []))
            
            return {
                "success": True,
                "message": f"OneDrive bağlantısı başarılı. {file_count} dosya bulundu.",
                "storage_type": "onedrive",
                "file_count": file_count
            }
        
        else:
            raise HTTPException(status_code=400, detail=f"Desteklenmeyen storage tipi: {storage_type}")
    
    except Exception as e:
        print(f"Bağlantı testi hatası: {e}")
        # Token süresi dolmuş olabilir, refresh dene
        try:
            from app.core.config import (
                GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET,
                ONEDRIVE_CLIENT_ID, ONEDRIVE_CLIENT_SECRET
            )
            
            refresh_token = user_storage.get('refresh_token')
            
            if storage_type == "google_drive" and refresh_token and GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET:
                adapter = GoogleDriveAdapter()
                tokens = adapter.refresh_access_token(
                    refresh_token=refresh_token,
                    client_id=GOOGLE_DRIVE_CLIENT_ID,
                    client_secret=GOOGLE_DRIVE_CLIENT_SECRET
                )
                db.collection("user_external_storage").document(current_user.id).update({
                    "access_token": tokens['access_token'],
                    "updated_at": datetime.now()
                })
                return {
                    "success": True,
                    "message": "Token yenilendi. Lütfen tekrar deneyin.",
                    "token_refreshed": True
                }
            elif storage_type == "onedrive" and refresh_token and ONEDRIVE_CLIENT_ID and ONEDRIVE_CLIENT_SECRET:
                adapter = OneDriveAdapter()
                tokens = adapter.refresh_access_token(
                    refresh_token=refresh_token,
                    client_id=ONEDRIVE_CLIENT_ID,
                    client_secret=ONEDRIVE_CLIENT_SECRET
                )
                db.collection("user_external_storage").document(current_user.id).update({
                    "access_token": tokens['access_token'],
                    "updated_at": datetime.now()
                })
                return {
                    "success": True,
                    "message": "Token yenilendi. Lütfen tekrar deneyin.",
                    "token_refreshed": True
                }
        except:
            pass
        
        raise HTTPException(
            status_code=500,
            detail=safe_error_message(e, "Bağlantı testi sırasında bir hata oluştu")
        )

