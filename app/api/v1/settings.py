# app/api/v1/settings.py
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from app.dependencies import get_current_admin_user
from app.schemas.user import UserInDB
from app.core.config import DEPLOYMENT_TYPE, ENVIRONMENT, DEBUG
from google.cloud import firestore
import os

def safe_error_message(e: Exception, default_message: str) -> str:
    """Production'da hassas bilgi sızıntısını önlemek için güvenli hata mesajı döndürür."""
    if ENVIRONMENT == "production" and not DEBUG:
        return default_message
    else:
        return f"{default_message}: {str(e)}"

router = APIRouter()

class DeploymentSettings(BaseModel):
    deployment_type: str

class MailSettings(BaseModel):
    email_address: str
    password: Optional[str] = None
    imap_server: Optional[str] = None
    imap_port: Optional[int] = 993
    smtp_server: Optional[str] = None
    smtp_port: Optional[int] = 587
    fetch_unread_only: bool = True  # Sadece okunmamış mailleri çek

class ExternalStorageSettings(BaseModel):
    """Harici depolama ayarları - Artık kullanılmıyor, sadece geriye dönük uyumluluk için"""
    # Not: Client ID/Secret artık environment variable'lardan alınıyor (config.py)
    # Not: Token'lar user_external_storage collection'ında saklanıyor (kullanıcı bazında)
    pass

# Mevcut ayarı okumak için
@router.get("/deployment", response_model=DeploymentSettings)
def get_deployment_settings(admin_user: UserInDB = Depends(get_current_admin_user)):
    """Mevcut dağıtım (deployment) ayarını döndürür."""
    return DeploymentSettings(deployment_type=DEPLOYMENT_TYPE)

# Ayarı güncellemek için
@router.put("/deployment", status_code=status.HTTP_202_ACCEPTED)
def update_deployment_settings(
    settings: DeploymentSettings,
    admin_user: UserInDB = Depends(get_current_admin_user)
):
    """
    Dağıtım ayarını günceller.
    NOT: Bu işlem, değişikliğin aktif olması için sunucunun yeniden başlatılmasını gerektirir.
    Bu endpoint, .env dosyasına yazar veya başka bir konfigürasyon yönetim aracını tetikler.
    """
    # Bu kısım, konfigürasyonunuzu nasıl yönettiğinize bağlıdır.
    # En basit yöntem, projenin kök dizininde bir .env dosyası yönetmektir.
    try:
        # Örnek: .env dosyasına yazma
        with open(".env", "w") as f:
            f.write(f"DEPLOYMENT_TYPE={settings.deployment_type}\n")
        
        # Kullanıcıya bir sonraki adımın ne olduğunu bildiren bir mesaj döndür.
        return {"message": "Ayar başarıyla güncellendi. Değişikliklerin etkili olması için lütfen sunucuyu yeniden başlatın."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=safe_error_message(e, "Ayar dosyası yazılırken bir hata oluştu"))

# Mail ayarları
@router.get("/mail", response_model=MailSettings)
def get_mail_settings(admin_user: UserInDB = Depends(get_current_admin_user)):
    """Mail konfigürasyonunu getirir."""
    db = firestore.Client()
    doc = db.collection("mail_settings").document(admin_user.tenant_id).get()
    if doc.exists:
        data = doc.to_dict()
        return MailSettings(**data)
    return MailSettings(email_address="")

@router.put("/mail", response_model=MailSettings)
def update_mail_settings(
    settings: MailSettings,
    admin_user: UserInDB = Depends(get_current_admin_user)
):
    """Mail konfigürasyonunu günceller."""
    db = firestore.Client()
    # Pydantic model'den dict'e çevir - exclude_none=False kullanarak False değerlerini de dahil et
    settings_dict = settings.model_dump(exclude_none=False)
    # fetch_unread_only'yi açıkça ekle (False değeri de dahil) - emin olmak için
    # None kontrolü yap - eğer None ise True yap, değilse bool'a çevir
    if settings.fetch_unread_only is None:
        settings_dict['fetch_unread_only'] = True
    else:
        settings_dict['fetch_unread_only'] = bool(settings.fetch_unread_only)
    print(f"💾 Mail ayarları kaydediliyor:")
    print(f"   - fetch_unread_only (model): {settings.fetch_unread_only} (tip: {type(settings.fetch_unread_only).__name__})")
    print(f"   - fetch_unread_only (dict): {settings_dict.get('fetch_unread_only')} (tip: {type(settings_dict.get('fetch_unread_only')).__name__})")
    print(f"   - Tüm ayarlar: {list(settings_dict.keys())}")
    print(f"   - settings_dict içeriği: {settings_dict}")
    # Firestore'a kaydet - merge=True kullanarak sadece gönderilen alanları güncelle
    db.collection("mail_settings").document(admin_user.tenant_id).set(settings_dict, merge=True)
    # Kaydedilen değeri hemen doğrula
    verify_doc = db.collection("mail_settings").document(admin_user.tenant_id).get()
    if verify_doc.exists:
        verify_data = verify_doc.to_dict()
        print(f"✅ Firestore'a kaydedildi - fetch_unread_only: {verify_data.get('fetch_unread_only')} (tip: {type(verify_data.get('fetch_unread_only')).__name__})")
        print(f"✅ Tüm Firestore verisi: {verify_data}")
    return settings

@router.post("/mail/test")
def test_mail_connection(admin_user: UserInDB = Depends(get_current_admin_user)):
    """Mail bağlantısını test eder."""
    from app.services.mail_service import test_mail_connection
    
    db = firestore.Client()
    doc = db.collection("mail_settings").document(admin_user.tenant_id).get()
    if not doc.exists:
        raise HTTPException(status_code=400, detail="Mail ayarları bulunamadı. Lütfen önce mail ayarlarını yapılandırın.")
    
    settings = doc.to_dict()
    email_address = settings.get("email_address", "")
    password = settings.get("password", "")
    imap_server = settings.get("imap_server", "")
    imap_port = settings.get("imap_port", 993)
    
    if not email_address:
        raise HTTPException(status_code=400, detail="Mail adresi belirtilmemiş.")
    if not password:
        raise HTTPException(status_code=400, detail="Mail şifresi belirtilmemiş.")
    
    success, message = test_mail_connection(email_address, password, imap_server, imap_port)
    
    return {
        "success": success,
        "message": message,
        "email_address": email_address,
        "imap_server": imap_server or "Otomatik tespit edilecek",
        "imap_port": imap_port
    }

# --- Harici Depolama Ayarları ---

@router.get("/external-storage", response_model=ExternalStorageSettings)
def get_external_storage_settings(admin_user: UserInDB = Depends(get_current_admin_user)):
    """Harici depolama ayarlarını getirir"""
    db = firestore.Client()
    doc = db.collection("external_storage_settings").document(admin_user.tenant_id).get()
    if doc.exists:
        data = doc.to_dict()
        # Firestore timestamp'leri datetime'a çevir
        if 'last_sync_at' in data and data['last_sync_at']:
            if hasattr(data['last_sync_at'], 'timestamp'):
                data['last_sync_at'] = datetime.fromtimestamp(data['last_sync_at'].timestamp())
            elif isinstance(data['last_sync_at'], str):
                try:
                    data['last_sync_at'] = datetime.fromisoformat(data['last_sync_at'].replace('Z', '+00:00'))
                except:
                    data['last_sync_at'] = None
        return ExternalStorageSettings(**data)
    return ExternalStorageSettings()

@router.put("/external-storage", response_model=ExternalStorageSettings)
def update_external_storage_settings(
    settings: ExternalStorageSettings,
    admin_user: UserInDB = Depends(get_current_admin_user)
):
    """Harici depolama ayarlarını günceller"""
    db = firestore.Client()
    settings_dict = settings.model_dump(exclude_none=True)
    # Password/secret alanlarını güvenli şekilde kaydet (eğer None ise mevcut değeri koru)
    existing_doc = db.collection("external_storage_settings").document(admin_user.tenant_id).get()
    if existing_doc.exists:
        existing_data = existing_doc.to_dict()
        # Eğer secret/token alanları None ise, mevcut değerleri koru
        sensitive_fields = [
            'google_drive_client_secret', 'google_drive_access_token', 'google_drive_refresh_token',
            'onedrive_client_secret', 'onedrive_access_token', 'onedrive_refresh_token'
        ]
        for field in sensitive_fields:
            if field not in settings_dict or settings_dict[field] is None:
                if field in existing_data:
                    settings_dict[field] = existing_data[field]
    
    db.collection("external_storage_settings").document(admin_user.tenant_id).set(settings_dict, merge=True)
    return settings

@router.get("/external-storage/auth-url")
def get_external_storage_auth_url(
    storage_type: str = Query(..., description="Storage tipi: 'google_drive' veya 'onedrive'"),
    admin_user: UserInDB = Depends(get_current_admin_user),
    request: Request = None
):
    """OAuth 2.0 authorization URL'i döndürür"""
    from app.storage_adapters.google_drive_adapter import GoogleDriveAdapter
    from app.storage_adapters.onedrive_adapter import OneDriveAdapter
    
    db = firestore.Client()
    doc = db.collection("external_storage_settings").document(admin_user.tenant_id).get()
    if not doc.exists:
        raise HTTPException(status_code=400, detail="Harici depolama ayarları bulunamadı. Lütfen önce ayarları yapılandırın.")
    
    settings_data = doc.to_dict()
    settings = ExternalStorageSettings(**settings_data) if settings_data else ExternalStorageSettings()
    
    # Base URL'i request'ten al veya environment variable'dan, yoksa request'ten oluştur
    base_url = os.environ.get("API_BASE_URL")
    if not base_url and request:
        # Request'ten base URL'i oluştur
        base_url = str(request.base_url).rstrip('/')
    elif not base_url:
        # Fallback: localhost (sadece development için)
        base_url = "http://localhost:8000"
    
    redirect_uri = f"{base_url}/api/v1/settings/external-storage/oauth-callback"
    
    if storage_type == "google_drive":
        if not settings.google_drive_client_id or not settings.google_drive_client_secret:
            raise HTTPException(
                status_code=400, 
                detail="Google Drive client ID ve secret gerekli. Lütfen önce ayarları yapılandırın."
            )
        adapter = GoogleDriveAdapter()
        auth_url = adapter.get_auth_url(
            client_id=settings.google_drive_client_id,
            client_secret=settings.google_drive_client_secret,
            redirect_uri=redirect_uri
        )
        return {"auth_url": auth_url, "storage_type": storage_type}
    
    elif storage_type == "onedrive":
        if not settings.onedrive_client_id or not settings.onedrive_client_secret:
            raise HTTPException(
                status_code=400, 
                detail="OneDrive client ID ve secret gerekli. Lütfen önce ayarları yapılandırın."
            )
        adapter = OneDriveAdapter()
        auth_url = adapter.get_auth_url(
            client_id=settings.onedrive_client_id,
            client_secret=settings.onedrive_client_secret,
            redirect_uri=redirect_uri
        )
        return {"auth_url": auth_url, "storage_type": storage_type}
    
    else:
        raise HTTPException(status_code=400, detail=f"Desteklenmeyen storage tipi: {storage_type}")

@router.get("/external-storage/oauth-callback")
def oauth_callback(
    code: str = Query(..., description="OAuth authorization code"),
    state: Optional[str] = Query(None, description="OAuth state parameter"),
    error: Optional[str] = Query(None, description="OAuth error"),
    admin_user: UserInDB = Depends(get_current_admin_user),
    request: Request = None
):
    """OAuth callback endpoint'i - authorization code'u token'lara çevirir"""
    from app.storage_adapters.google_drive_adapter import GoogleDriveAdapter
    from app.storage_adapters.onedrive_adapter import OneDriveAdapter
    
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth hatası: {error}")
    
    if not code:
        raise HTTPException(status_code=400, detail="Authorization code bulunamadı")
    
    db = firestore.Client()
    doc = db.collection("external_storage_settings").document(admin_user.tenant_id).get()
    if not doc.exists:
        raise HTTPException(status_code=400, detail="Harici depolama ayarları bulunamadı")
    
    settings_data = doc.to_dict()
    settings = ExternalStorageSettings(**settings_data) if settings_data else ExternalStorageSettings()
    
    # Storage type'ı state'ten veya mevcut ayarlardan belirle
    storage_type = state or settings.storage_type
    if not storage_type:
        raise HTTPException(status_code=400, detail="Storage tipi belirlenemedi")
    
    # Base URL'i request'ten al veya environment variable'dan, yoksa request'ten oluştur
    base_url = os.environ.get("API_BASE_URL")
    if not base_url and request:
        # Request'ten base URL'i oluştur
        base_url = str(request.base_url).rstrip('/')
    elif not base_url:
        # Fallback: localhost (sadece development için)
        base_url = "http://localhost:8000"
    
    redirect_uri = f"{base_url}/api/v1/settings/external-storage/oauth-callback"
    
    try:
        if storage_type == "google_drive":
            if not settings.google_drive_client_id or not settings.google_drive_client_secret:
                raise HTTPException(status_code=400, detail="Google Drive ayarları eksik")
            
            adapter = GoogleDriveAdapter()
            tokens = adapter.exchange_code_for_tokens(
                code=code,
                client_id=settings.google_drive_client_id,
                client_secret=settings.google_drive_client_secret,
                redirect_uri=redirect_uri
            )
            
            # Token'ları ayarlara kaydet
            settings.google_drive_access_token = tokens['access_token']
            settings.google_drive_refresh_token = tokens.get('refresh_token', settings.google_drive_refresh_token)
            settings.storage_type = "google_drive"
            settings.is_enabled = True
            
        elif storage_type == "onedrive":
            if not settings.onedrive_client_id or not settings.onedrive_client_secret:
                raise HTTPException(status_code=400, detail="OneDrive ayarları eksik")
            
            adapter = OneDriveAdapter()
            tokens = adapter.exchange_code_for_tokens(
                code=code,
                client_id=settings.onedrive_client_id,
                client_secret=settings.onedrive_client_secret,
                redirect_uri=redirect_uri
            )
            
            # Token'ları ayarlara kaydet
            settings.onedrive_access_token = tokens['access_token']
            settings.onedrive_refresh_token = tokens.get('refresh_token', settings.onedrive_refresh_token)
            settings.storage_type = "onedrive"
            settings.is_enabled = True
        
        else:
            raise HTTPException(status_code=400, detail=f"Desteklenmeyen storage tipi: {storage_type}")
        
        # Ayarları kaydet
        settings_dict = settings.model_dump(exclude_none=True)
        db.collection("external_storage_settings").document(admin_user.tenant_id).set(settings_dict, merge=True)
        
        return {
            "success": True,
            "message": f"{storage_type} bağlantısı başarıyla kuruldu.",
            "storage_type": storage_type
        }
    
    except Exception as e:
        print(f"OAuth callback hatası: {e}")
        raise HTTPException(
            status_code=500, 
            detail=safe_error_message(e, f"{storage_type} bağlantısı kurulurken bir hata oluştu")
        )

@router.post("/external-storage/sync")
async def trigger_external_storage_sync(admin_user: UserInDB = Depends(get_current_admin_user)):
    """Manuel senkronizasyon tetikler"""
    from app.services.external_storage_sync import sync_external_storage
    from app.dependencies import get_db_repository
    
    db = get_db_repository()
    
    firestore_db = firestore.Client()
    doc = firestore_db.collection("external_storage_settings").document(admin_user.tenant_id).get()
    if not doc.exists:
        raise HTTPException(status_code=400, detail="Harici depolama ayarları bulunamadı")
    
    settings_data = doc.to_dict()
    settings = ExternalStorageSettings(**settings_data) if settings_data else ExternalStorageSettings()
    
    if not settings.is_enabled or not settings.storage_type:
        raise HTTPException(status_code=400, detail="Harici depolama bağlantısı aktif değil")
    
    try:
        await sync_external_storage(admin_user.tenant_id, settings, db)
        
        # Son senkronizasyon zamanını güncelle
        settings.last_sync_at = datetime.now()
        settings_dict = settings.model_dump(exclude_none=True)
        firestore_db.collection("external_storage_settings").document(admin_user.tenant_id).update({
            "last_sync_at": settings.last_sync_at
        })
        
        return {
            "success": True,
            "message": f"{settings.storage_type} senkronizasyonu tamamlandı.",
            "last_sync_at": settings.last_sync_at.isoformat()
        }
    
    except Exception as e:
        print(f"Senkronizasyon hatası: {e}")
        raise HTTPException(
            status_code=500,
            detail=safe_error_message(e, "Senkronizasyon sırasında bir hata oluştu")
        )

@router.get("/external-storage/test")
def test_external_storage_connection(admin_user: UserInDB = Depends(get_current_admin_user)):
    """Harici depolama bağlantısını test eder"""
    from app.storage_adapters.google_drive_adapter import GoogleDriveAdapter
    from app.storage_adapters.onedrive_adapter import OneDriveAdapter
    
    db = firestore.Client()
    doc = db.collection("external_storage_settings").document(admin_user.tenant_id).get()
    if not doc.exists:
        raise HTTPException(status_code=400, detail="Harici depolama ayarları bulunamadı")
    
    settings_data = doc.to_dict()
    settings = ExternalStorageSettings(**settings_data) if settings_data else ExternalStorageSettings()
    
    if not settings.is_enabled or not settings.storage_type:
        raise HTTPException(status_code=400, detail="Harici depolama bağlantısı aktif değil")
    
    try:
        if settings.storage_type == "google_drive":
            if not settings.google_drive_access_token:
                raise HTTPException(status_code=400, detail="Google Drive access token bulunamadı")
            
            adapter = GoogleDriveAdapter()
            # Root klasörü listele (test için)
            result = adapter.list_files(access_token=settings.google_drive_access_token)
            file_count = len(result.get('files', []))
            
            return {
                "success": True,
                "message": f"Google Drive bağlantısı başarılı. {file_count} dosya bulundu.",
                "storage_type": "google_drive",
                "file_count": file_count
            }
        
        elif settings.storage_type == "onedrive":
            if not settings.onedrive_access_token:
                raise HTTPException(status_code=400, detail="OneDrive access token bulunamadı")
            
            adapter = OneDriveAdapter()
            # Root klasörü listele (test için)
            result = adapter.list_files(access_token=settings.onedrive_access_token)
            file_count = len(result.get('files', []))
            
            return {
                "success": True,
                "message": f"OneDrive bağlantısı başarılı. {file_count} dosya bulundu.",
                "storage_type": "onedrive",
                "file_count": file_count
            }
        
        else:
            raise HTTPException(status_code=400, detail=f"Desteklenmeyen storage tipi: {settings.storage_type}")
    
    except Exception as e:
        print(f"Bağlantı testi hatası: {e}")
        # Token süresi dolmuş olabilir, refresh dene
        try:
            if settings.storage_type == "google_drive" and settings.google_drive_refresh_token:
                adapter = GoogleDriveAdapter()
                tokens = adapter.refresh_access_token(
                    refresh_token=settings.google_drive_refresh_token,
                    client_id=settings.google_drive_client_id,
                    client_secret=settings.google_drive_client_secret
                )
                settings.google_drive_access_token = tokens['access_token']
                settings_dict = settings.model_dump(exclude_none=True)
                db.collection("external_storage_settings").document(admin_user.tenant_id).set(settings_dict, merge=True)
                return {
                    "success": True,
                    "message": "Token yenilendi. Lütfen tekrar deneyin.",
                    "token_refreshed": True
                }
            elif settings.storage_type == "onedrive" and settings.onedrive_refresh_token:
                adapter = OneDriveAdapter()
                tokens = adapter.refresh_access_token(
                    refresh_token=settings.onedrive_refresh_token,
                    client_id=settings.onedrive_client_id,
                    client_secret=settings.onedrive_client_secret
                )
                settings.onedrive_access_token = tokens['access_token']
                settings_dict = settings.model_dump(exclude_none=True)
                db.collection("external_storage_settings").document(admin_user.tenant_id).set(settings_dict, merge=True)
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