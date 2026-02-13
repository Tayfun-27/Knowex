# backend/app/services/external_storage_sync.py

import hashlib
from typing import Dict, Any, Optional
from datetime import datetime
from app.api.v1.settings import ExternalStorageSettings
from app.storage_adapters.google_drive_adapter import GoogleDriveAdapter
from app.storage_adapters.onedrive_adapter import OneDriveAdapter
from app.repositories.base import BaseRepository
from app.schemas.file import FileOut
from app.schemas.user import UserInDB
from app.services.vector_service import index_file
from app.storage_adapters.base import BaseStorageAdapter
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
import io

async def sync_external_storage(
    tenant_id: str,
    settings: ExternalStorageSettings,
    db: BaseRepository
):
    """
    Harici depolamadan dosyaları senkronize eder.
    
    Args:
        tenant_id: Tenant ID
        settings: ExternalStorageSettings objesi
        db: Database repository
    """
    if not settings.is_enabled or not settings.storage_type:
        raise Exception("Harici depolama bağlantısı aktif değil")
    
    # Adapter'ı seç
    adapter = None
    access_token = None
    refresh_token = None
    client_id = None
    client_secret = None
    folder_id = None
    
    if settings.storage_type == "google_drive":
        adapter = GoogleDriveAdapter()
        access_token = settings.google_drive_access_token
        refresh_token = settings.google_drive_refresh_token
        client_id = settings.google_drive_client_id
        client_secret = settings.google_drive_client_secret
        folder_id = settings.google_drive_folder_id
    elif settings.storage_type == "onedrive":
        adapter = OneDriveAdapter()
        access_token = settings.onedrive_access_token
        refresh_token = settings.onedrive_refresh_token
        client_id = settings.onedrive_client_id
        client_secret = settings.onedrive_client_secret
        folder_id = settings.onedrive_folder_id
    else:
        raise Exception(f"Desteklenmeyen storage tipi: {settings.storage_type}")
    
    if not access_token:
        raise Exception("Access token bulunamadı. Lütfen önce OAuth bağlantısını kurun.")
    
    # Token'ı kontrol et ve gerekirse yenile
    try:
        # Test için bir dosya listesi çek
        adapter.list_files(folder_id=folder_id, access_token=access_token, page_token=None)
    except Exception as e:
        # Token süresi dolmuş olabilir, refresh dene
        if refresh_token and client_id and client_secret:
            print(f"Token süresi dolmuş, yenileniyor...")
            tokens = adapter.refresh_access_token(
                refresh_token=refresh_token,
                client_id=client_id,
                client_secret=client_secret
            )
            access_token = tokens['access_token']
            # Yeni token'ı kaydet
            firestore_db = firestore.Client()
            update_data = {}
            if settings.storage_type == "google_drive":
                update_data['google_drive_access_token'] = access_token
                if 'refresh_token' in tokens:
                    update_data['google_drive_refresh_token'] = tokens['refresh_token']
            elif settings.storage_type == "onedrive":
                update_data['onedrive_access_token'] = access_token
                if 'refresh_token' in tokens:
                    update_data['onedrive_refresh_token'] = tokens['refresh_token']
            
            firestore_db.collection("external_storage_settings").document(tenant_id).update(update_data)
            print(f"Token başarıyla yenilendi.")
        else:
            raise Exception(f"Token geçersiz ve refresh token bulunamadı: {e}")
    
    # Tüm dosyaları çek (pagination ile)
    all_files = []
    page_token = None
    
    while True:
        result = adapter.list_files(
            folder_id=folder_id,
            access_token=access_token,
            page_token=page_token
        )
        
        all_files.extend(result.get('files', []))
        page_token = result.get('next_page_token')
        
        if not page_token:
            break
    
    print(f"📦 {len(all_files)} dosya bulundu ({settings.storage_type})")
    
    # Firestore'dan mevcut external storage dosyalarını al
    firestore_db = firestore.Client()
    external_files_col = firestore_db.collection("external_storage_files")
    
    # Tenant'a ait external storage dosyalarını al
    existing_files_query = external_files_col.where(
        filter=FieldFilter("tenant_id", "==", tenant_id)
    ).where(
        filter=FieldFilter("storage_type", "==", settings.storage_type)
    ).stream()
    
    existing_files_map = {}
    for doc in existing_files_query:
        data = doc.to_dict()
        external_file_id = data.get('external_file_id')  # Google Drive/OneDrive file ID
        existing_files_map[external_file_id] = {
            'doc_id': doc.id,
            'file_id': data.get('file_id'),  # Internal file ID (files collection'daki)
            'last_modified': data.get('last_modified'),
            'size': data.get('size')
        }
    
    # Dosyaları işle
    synced_count = 0
    updated_count = 0
    skipped_count = 0
    error_count = 0
    
    # User objesi oluştur (index_file için gerekli)
    # Not: Bu geçici bir user objesi, sadece tenant_id için
    user = UserInDB(
        id="system",
        email="system@sync",
        tenant_id=tenant_id,
        roles=["Admin"],
        is_active=True
    )
    
    # NOT: Artık dosyaları kendi storage'ımıza kaydetmiyoruz
    # Storage adapter'a ihtiyaç yok, sadece metadata kaydediyoruz
    
    for external_file in all_files:
        external_file_id = external_file['id']
        file_name = external_file['name']
        file_size = external_file['size']
        modified_time = external_file['modified_time']
        
        # Dosya zaten var mı ve güncel mi?
        if external_file_id in existing_files_map:
            existing = existing_files_map[external_file_id]
            existing_modified = existing.get('last_modified')
            existing_size = existing.get('size')
            
            # Timestamp karşılaştırması
            if isinstance(existing_modified, datetime):
                existing_ts = existing_modified
            elif isinstance(existing_modified, str):
                try:
                    existing_ts = datetime.fromisoformat(existing_modified.replace('Z', '+00:00'))
                except:
                    existing_ts = None
            else:
                existing_ts = None
            
            # Eğer dosya değişmemişse atla
            if existing_ts and modified_time and existing_ts >= modified_time and existing_size == file_size:
                skipped_count += 1
                continue
        
        # Dosyayı sadece metadata olarak kaydet (indirme yapmıyoruz)
        try:
            print(f"📋 Metadata kaydediliyor: {file_name}")
            original_mime_type = external_file.get('mime_type', '')
            
            # Google Workspace dosyaları için MIME type ve dosya adını düzenle
            mime_type = original_mime_type
            if original_mime_type.startswith('application/vnd.google-apps.'):
                if original_mime_type == 'application/vnd.google-apps.document':
                    mime_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                    if not file_name.endswith('.docx') and not file_name.endswith('.doc'):
                        file_name = file_name + '.docx'
                elif original_mime_type == 'application/vnd.google-apps.spreadsheet':
                    mime_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                    if not file_name.endswith('.xlsx') and not file_name.endswith('.xls'):
                        file_name = file_name + '.xlsx'
                elif original_mime_type == 'application/vnd.google-apps.presentation':
                    mime_type = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
                    if not file_name.endswith('.pptx') and not file_name.endswith('.ppt'):
                        file_name = file_name + '.pptx'
                elif original_mime_type == 'application/vnd.google-apps.drawing':
                    mime_type = 'image/png'
                    if not file_name.endswith('.png'):
                        file_name = file_name + '.png'
            
            # Önce bir folder oluştur veya mevcut external storage folder'ını bul
            folder_id = _get_or_create_external_storage_folder(tenant_id, settings.storage_type, db)
            
            # File record oluştur (storage_path=None, external storage bilgileri ile)
            from app.schemas.file import FileCreate
            file_data = FileCreate(
                name=file_name,
                folder_id=folder_id,
                content_type=mime_type,
                size=file_size,
                owner_id=user.id,
                tenant_id=tenant_id,
                storage_path=None,  # Artık kendi storage'ımızda tutmuyoruz
                external_file_id=external_file_id,  # Google Drive/OneDrive file ID
                external_storage_type=settings.storage_type,  # "google_drive" veya "onedrive"
                created_at=datetime.now()
            )
            
            file_record = db.create_file_record(file_data)
            
            # External storage mapping kaydı oluştur/güncelle
            external_file_doc_id = existing_files_map.get(external_file_id, {}).get('doc_id')
            if external_file_doc_id:
                external_files_col.document(external_file_doc_id).update({
                    'file_id': file_record.id,
                    'last_modified': modified_time,
                    'size': file_size,
                    'updated_at': datetime.now()
                })
                updated_count += 1
            else:
                external_files_col.add({
                    'tenant_id': tenant_id,
                    'storage_type': settings.storage_type,
                    'external_file_id': external_file_id,
                    'file_id': file_record.id,
                    'file_name': file_name,
                    'last_modified': modified_time,
                    'size': file_size,
                    'web_view_link': external_file.get('web_view_link', ''),
                    'created_at': datetime.now(),
                    'updated_at': datetime.now()
                })
                synced_count += 1
            
            # NOT: İndeksleme şimdilik atlanıyor - dosya açıldığında veya chat'te kullanıldığında 
            # Google Drive'dan geçici olarak indirilip indexlenecek
            print(f"ℹ️ Dosya metadata kaydedildi (indeksleme için dosya açıldığında Google Drive'dan indirilecek): {file_name}")
        
        except Exception as e:
            print(f"❌ Dosya işleme hatası ({file_name}): {e}")
            error_count += 1
            continue
    
    print(f"✅ Senkronizasyon tamamlandı:")
    print(f"   - Yeni: {synced_count}")
    print(f"   - Güncellenen: {updated_count}")
    print(f"   - Atlanan: {skipped_count}")
    print(f"   - Hata: {error_count}")
    
    return {
        "synced": synced_count,
        "updated": updated_count,
        "skipped": skipped_count,
        "errors": error_count,
        "total": len(all_files)
    }


def _get_extension_from_mime_type(mime_type: str, file_name: str) -> str:
    """MIME type'dan veya dosya adından extension çıkarır"""
    if '.' in file_name:
        return file_name.split('.')[-1].lower()
    
    mime_to_ext = {
        'application/pdf': 'pdf',
        'application/msword': 'doc',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
        'application/vnd.ms-excel': 'xls',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
        'application/vnd.ms-powerpoint': 'ppt',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
        'text/plain': 'txt',
        'text/html': 'html',
        'image/jpeg': 'jpg',
        'image/png': 'png',
        'image/gif': 'gif',
    }
    
    return mime_to_ext.get(mime_type, 'bin')


def _get_or_create_external_storage_folder(tenant_id: str, storage_type: str, db: BaseRepository) -> str:
    """External storage için klasör oluşturur veya mevcut olanı döndürür"""
    from app.schemas.folder import FolderCreate
    from app.schemas.user import UserInDB
    
    folder_name = f"External Storage - {storage_type.title()}"
    
    # Root klasöründe ara
    folders = db.get_folders_by_parent(tenant_id=tenant_id, parent_id=None)
    
    for folder in folders:
        if folder.name == folder_name:
            return folder.id
    
    # Klasör yoksa oluştur
    user = UserInDB(
        id="system",
        email="system@sync",
        tenant_id=tenant_id,
        roles=["Admin"],
        is_active=True
    )
    
    folder_data = FolderCreate(name=folder_name, parent_id=None)
    new_folder = db.create_folder(
        folder_data=folder_data,
        owner_id=user.id,
        tenant_id=tenant_id
    )
    
    return new_folder.id

