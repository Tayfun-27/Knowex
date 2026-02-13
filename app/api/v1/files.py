# backend/app/api/v1/files.py

from fastapi import APIRouter, Depends, UploadFile, File, Form, Query, status, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse
import io
import mimetypes
from typing import List, Optional
# --- DÜZELTME: FileRename'i de import ediyoruz ---
from app.schemas.file import FileOut, FileMove, FileRename
from app.schemas.user import UserInDB
from app.services import file_service
from app.dependencies import get_current_user, get_db_repository, get_storage_adapter, get_current_admin_user, get_current_user_from_query_or_header
from app.repositories.base import BaseRepository
from app.storage_adapters.base import BaseStorageAdapter
from app.schemas.mention import MentionResponse, MentionableItem
from pydantic import BaseModel
import tempfile
import os
import subprocess
import shutil
from urllib.parse import quote
from pathlib import Path
from slowapi import Limiter
from slowapi.util import get_remote_address
from google.cloud.firestore_v1.base_query import FieldFilter

limiter = Limiter(key_func=get_remote_address)

router = APIRouter()


def _build_content_disposition(filename: str, disposition: str = "inline") -> str:
    """RFC 5987 uyumlu Content-Disposition header üretir."""
    base_name, ext = os.path.splitext(filename)
    ascii_base = base_name.encode("ascii", "ignore").decode() or "file"
    ascii_ext = ext.encode("ascii", "ignore").decode()
    ascii_name = f"{ascii_base}{ascii_ext}".replace('"', '')
    encoded_original = quote(filename)
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_original}"

def _download_from_external_storage(file_record: FileOut, user: UserInDB) -> bytes:
    """
    External storage'dan (Google Drive/OneDrive) dosyayı indirir.
    Token'ı kontrol eder ve gerekirse yeniler.
    """
    from google.cloud import firestore
    from app.storage_adapters.google_drive_adapter import GoogleDriveAdapter
    from app.storage_adapters.onedrive_adapter import OneDriveAdapter
    from app.core.config import GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET, ONEDRIVE_CLIENT_ID, ONEDRIVE_CLIENT_SECRET
    
    if not file_record.external_file_id or not file_record.external_storage_type:
        raise ValueError("Dosya external storage dosyası değil")
    
    firestore_db = firestore.Client()
    storage_type = file_record.external_storage_type
    
    # Kullanıcının storage bağlantısını al
    if storage_type == "google_drive":
        user_storage = firestore_db.collection("user_external_storage").document(user.id).get()
        if not user_storage.exists:
            # Admin seviyesinde bağlantıyı kontrol et
            admin_settings = firestore_db.collection("external_storage_settings").document(user.tenant_id).get()
            if not admin_settings.exists:
                raise HTTPException(status_code=404, detail="Google Drive bağlantısı bulunamadı")
            admin_data = admin_settings.to_dict()
            access_token = admin_data.get('google_drive_access_token')
            refresh_token = admin_data.get('google_drive_refresh_token')
            client_id = GOOGLE_DRIVE_CLIENT_ID
            client_secret = GOOGLE_DRIVE_CLIENT_SECRET
        else:
            storage_data = user_storage.to_dict()
            access_token = storage_data.get('access_token')
            refresh_token = storage_data.get('refresh_token')
            client_id = GOOGLE_DRIVE_CLIENT_ID
            client_secret = GOOGLE_DRIVE_CLIENT_SECRET
        
        adapter = GoogleDriveAdapter()
    elif storage_type == "onedrive":
        user_storage = firestore_db.collection("user_external_storage").document(user.id).get()
        if not user_storage.exists:
            # Admin seviyesinde bağlantıyı kontrol et
            admin_settings = firestore_db.collection("external_storage_settings").document(user.tenant_id).get()
            if not admin_settings.exists:
                raise HTTPException(status_code=404, detail="OneDrive bağlantısı bulunamadı")
            admin_data = admin_settings.to_dict()
            access_token = admin_data.get('onedrive_access_token')
            refresh_token = admin_data.get('onedrive_refresh_token')
            client_id = ONEDRIVE_CLIENT_ID
            client_secret = ONEDRIVE_CLIENT_SECRET
        else:
            storage_data = user_storage.to_dict()
            access_token = storage_data.get('access_token')
            refresh_token = storage_data.get('refresh_token')
            client_id = ONEDRIVE_CLIENT_ID
            client_secret = ONEDRIVE_CLIENT_SECRET
        
        adapter = OneDriveAdapter()
    else:
        raise HTTPException(status_code=400, detail=f"Desteklenmeyen storage tipi: {storage_type}")
    
    if not access_token:
        raise HTTPException(status_code=404, detail=f"{storage_type} bağlantısı bulunamadı")
    
    # Token'ı kontrol et ve gerekirse yenile
    try:
        if storage_type == "google_drive":
            content_bytes = adapter.download_file(
                file_id=file_record.external_file_id,
                access_token=access_token,
                mime_type=file_record.content_type
            )
        else:  # onedrive
            content_bytes = adapter.download_file(
                file_id=file_record.external_file_id,
                access_token=access_token
            )
    except Exception as e:
        # Token süresi dolmuş olabilir
        if refresh_token and client_id and client_secret:
            try:
                tokens = adapter.refresh_access_token(
                    refresh_token=refresh_token,
                    client_id=client_id,
                    client_secret=client_secret
                )
                access_token = tokens['access_token']
                
                # Token'ı güncelle
                if user_storage.exists:
                    firestore_db.collection("user_external_storage").document(user.id).update({
                        'access_token': access_token
                    })
                else:
                    # Admin seviyesinde güncelle
                    update_data = {}
                    if storage_type == "google_drive":
                        update_data['google_drive_access_token'] = access_token
                    else:
                        update_data['onedrive_access_token'] = access_token
                    firestore_db.collection("external_storage_settings").document(user.tenant_id).update(update_data)
                
                # Tekrar dene
                if storage_type == "google_drive":
                    content_bytes = adapter.download_file(
                        file_id=file_record.external_file_id,
                        access_token=access_token,
                        mime_type=file_record.content_type
                    )
                else:
                    content_bytes = adapter.download_file(
                        file_id=file_record.external_file_id,
                        access_token=access_token
                    )
            except Exception as refresh_error:
                raise HTTPException(status_code=500, detail=f"Dosya indirilemedi (token yenileme başarısız): {refresh_error}")
        else:
            raise HTTPException(status_code=500, detail=f"Dosya indirilemedi: {e}")
    
    return content_bytes

@router.post("/upload", response_model=FileOut, status_code=201)
@limiter.limit("20/minute")  # Rate limiting: 20 dosya/dakika
def upload_file(
    request: Request,
    folder_id: Optional[str] = Form(None), 
    file: UploadFile = File(...),
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository),
    storage: BaseStorageAdapter = Depends(get_storage_adapter)
):
    return file_service.upload_new_file(
        file=file, folder_id=folder_id, user=current_user, db=db, storage=storage
    )

@router.get("/", response_model=List[FileOut])
def list_files_in_folder(
    folder_id: Optional[str] = Query(None),
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    return file_service.get_files_in_folder(
        user=current_user, folder_id=folder_id, db=db
    )

@router.put("/{file_id}/move", status_code=204)
def move_file(
    file_id: str,
    move_request: FileMove,
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    file_service.move_file_service(
        user=current_user, file_id=file_id, new_folder_id=move_request.new_folder_id, db=db
    )
    return

@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(
    file_id: str,
    admin_user: UserInDB = Depends(get_current_admin_user),
    db: BaseRepository = Depends(get_db_repository),
    storage: BaseStorageAdapter = Depends(get_storage_adapter)
):
    file_service.delete_file_service(
        user=admin_user, file_id=file_id, db=db, storage=storage
    )
    return
    
@router.get("/mention-list", response_model=MentionResponse)
def get_mention_list(
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    items = []
    all_tenant_folders = db.get_all_folders_for_tenant(tenant_id=current_user.tenant_id)
    folder_map = {folder.id: folder.name for folder in all_tenant_folders}
    if current_user.role == "Admin":
        files = db.get_all_files_for_tenant(tenant_id=current_user.tenant_id)
        for file in files:
            parent_folder_name = folder_map.get(file.folder_id)
            display_name = f"{parent_folder_name}/{file.name}" if parent_folder_name else file.name
            items.append(MentionableItem(id=file.id, name=display_name, type="file"))
        for folder in all_tenant_folders:
            items.append(MentionableItem(id=folder.id, name=folder.name, type="folder"))
    else:
        user_role = db.get_role_by_name(tenant_id=current_user.tenant_id, role_name=current_user.role)
        allowed_folder_ids = set(user_role.allowed_folders) if user_role else set()
        allowed_file_ids = set(user_role.allowed_files) if user_role else set()
        for folder_id in allowed_folder_ids:
            if folder_id in folder_map:
                items.append(MentionableItem(id=folder_id, name=folder_map[folder_id], type="folder"))
        all_tenant_files = db.get_all_files_for_tenant(tenant_id=current_user.tenant_id)
        for file in all_tenant_files:
            is_owner = file.owner_id == current_user.id
            is_file_allowed = file.id in allowed_file_ids
            is_folder_allowed = file.folder_id and file.folder_id in allowed_folder_ids
            if is_owner or is_file_allowed or is_folder_allowed:
                parent_folder_name = folder_map.get(file.folder_id)
                display_name = f"{parent_folder_name}/{file.name}" if parent_folder_name else file.name
                items.append(MentionableItem(id=file.id, name=display_name, type="file"))
    
    # YENİ: Veritabanı bağlantılarını ekle
    try:
        from app.api.v1.databases import get_database_connections
        db_connections = get_database_connections(current_user)
        for db_conn in db_connections:
            items.append(MentionableItem(
                id=db_conn.id,
                name=db_conn.name,
                type="database",
                db_type=db_conn.type
            ))
    except Exception as e:
        print(f"Veritabanı bağlantıları mention listesine eklenirken hata: {e}")
    
    # YENİ: API entegrasyonlarını ekle (sadece Admin kullanıcılar için)
    try:
        if current_user.role == "Admin":
            from google.cloud import firestore
            db = firestore.Client()
            integrations_ref = db.collection("api_integrations")
            query = integrations_ref.where(filter=FieldFilter("tenant_id", "==", current_user.tenant_id)).where(filter=FieldFilter("is_active", "==", True))
            docs = query.stream()
            
            for doc in docs:
                data = doc.to_dict()
                items.append(MentionableItem(
                    id=doc.id,
                    name=data.get("name", "API Entegrasyonu"),
                    type="api_integration",
                    description=data.get("description")
                ))
    except Exception as e:
        print(f"API entegrasyonları mention listesine eklenirken hata: {e}")
    
    return MentionResponse(items=items)

class SearchResultItem(BaseModel):
    """Arama sonucu item'i"""
    id: str
    name: str
    type: str  # "file" or "folder"
    folder_path: Optional[str] = None  # Klasör yolu (dosyalar için)
    parent_id: Optional[str] = None  # Parent klasör ID'si (klasörler için)

class SearchResponse(BaseModel):
    """Arama sonuçları"""
    files: List[SearchResultItem]
    folders: List[SearchResultItem]

@router.get("/search", response_model=SearchResponse)
def search_files_and_folders(
    query: str = Query(..., description="Arama terimi"),
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """Dosya ve klasörlerde arama yapar."""
    query_lower = query.lower().strip()
    if not query_lower or len(query_lower) < 2:
        return SearchResponse(files=[], folders=[])
    
    # Klasör yolu map'i oluştur
    all_tenant_folders = db.get_all_folders_for_tenant(tenant_id=current_user.tenant_id)
    
    def get_folder_path(folder_id: Optional[str], folder_map: dict) -> str:
        """Klasörün tam yolunu döndürür (üst klasörler dahil)"""
        if not folder_id:
            return ""
        path_parts = []
        current_id = folder_id
        visited = set()  # Sonsuz döngü önleme
        
        while current_id and current_id not in visited:
            visited.add(current_id)
            folder = folder_map.get(current_id)
            if not folder:
                break
            path_parts.insert(0, folder.name)
            # FolderOut schema'sında parent_id var
            current_id = getattr(folder, 'parent_id', None)
        
        return "/".join(path_parts) if len(path_parts) > 1 else ""
    
    folder_map = {folder.id: folder for folder in all_tenant_folders}
    
    matched_files = []
    matched_folders = []
    
    # Kullanıcı yetkilerini kontrol et
    if current_user.role == "Admin":
        # Tüm dosyaları ve klasörleri ara
        all_files = db.get_all_files_for_tenant(tenant_id=current_user.tenant_id)
        for file in all_files:
            if query_lower in file.name.lower():
                folder_path = get_folder_path(file.folder_id, folder_map) if file.folder_id else ""
                matched_files.append(SearchResultItem(
                    id=file.id,
                    name=file.name,
                    type="file",
                    folder_path=folder_path if folder_path else None
                ))
        
        for folder in all_tenant_folders:
            if query_lower in folder.name.lower():
                parent_id = getattr(folder, 'parent_id', None)
                folder_path = get_folder_path(parent_id, folder_map) if parent_id else ""
                matched_folders.append(SearchResultItem(
                    id=folder.id,
                    name=folder.name,
                    type="folder",
                    folder_path=folder_path if folder_path else None,
                    parent_id=parent_id  # Parent ID'yi ekle
                ))
    else:
        # Kullanıcı yetkilerine göre filtrele
        user_role = db.get_role_by_name(tenant_id=current_user.tenant_id, role_name=current_user.role)
        allowed_folder_ids = set(user_role.allowed_folders) if user_role else set()
        allowed_file_ids = set(user_role.allowed_files) if user_role else set()
        
        all_files = db.get_all_files_for_tenant(tenant_id=current_user.tenant_id)
        for file in all_files:
            is_owner = file.owner_id == current_user.id
            is_file_allowed = file.id in allowed_file_ids
            is_folder_allowed = file.folder_id and file.folder_id in allowed_folder_ids
            
            if (is_owner or is_file_allowed or is_folder_allowed) and query_lower in file.name.lower():
                folder_path = get_folder_path(file.folder_id, folder_map) if file.folder_id else ""
                matched_files.append(SearchResultItem(
                    id=file.id,
                    name=file.name,
                    type="file",
                    folder_path=folder_path if folder_path else None
                ))
        
        for folder_id in allowed_folder_ids:
            folder = folder_map.get(folder_id)
            if folder and query_lower in folder.name.lower():
                parent_id = getattr(folder, 'parent_id', None)
                folder_path = get_folder_path(parent_id, folder_map) if parent_id else ""
                matched_folders.append(SearchResultItem(
                    id=folder.id,
                    name=folder.name,
                    type="folder",
                    folder_path=folder_path if folder_path else None,
                    parent_id=parent_id  # Parent ID'yi ekle
                ))
    
    return SearchResponse(files=matched_files, folders=matched_folders)

class PreviewLinkOut(BaseModel):
    url: str

@router.get("/{file_id}/preview-url", response_model=PreviewLinkOut)
def get_file_preview_url(
    file_id: str,
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository),
    storage: BaseStorageAdapter = Depends(get_storage_adapter),
    request: Request = None
):
    """Bir dosya için geçici, güvenli bir ön izleme/indirme URL'i oluşturur."""
    file_record = db.get_file_by_id(tenant_id=current_user.tenant_id, file_id=file_id)
    
    if not file_record:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı veya bu firmaya ait değil.")

    has_permission = False
    
    if current_user.role == "Admin":
        has_permission = True
    
    elif file_record.owner_id == current_user.id:
        has_permission = True
        
    else:
        user_role = db.get_role_by_name(tenant_id=current_user.tenant_id, role_name=current_user.role)
        if user_role:
            allowed_file_ids = set(user_role.allowed_files)
            allowed_folder_ids = set(user_role.allowed_folders)
            
            if (file_record.id in allowed_file_ids or 
                (file_record.folder_id and file_record.folder_id in allowed_folder_ids)):
                has_permission = True

    if not has_permission:
        raise HTTPException(status_code=403, detail="Bu dosyayı görüntüleme yetkiniz yok.")
    
    # Eğer dosya external storage'dan geliyorsa, download endpoint'ini kullan
    if file_record.external_file_id and file_record.external_storage_type:
        base = str(request.base_url).rstrip("/") if request else ""
        absolute_url = f"{base}/api/v1/files/{file_id}/download"
        return PreviewLinkOut(url=absolute_url)
    
    # Normal dosyalar için mevcut mantık
    download_url = storage.get_download_url(storage_path=file_record.storage_path)
    # Lokal depolamada relatif bir yol dönüyor olabilir; mutlak ve çalışan bir indirme endpointi verelim
    if download_url.startswith("/download/"):
        # Kendi indirme endpointimize yönlendirelim (mutlak URL)
        base = str(request.base_url).rstrip("/") if request else ""
        absolute_url = f"{base}/api/v1/files/{file_id}/download"
        return PreviewLinkOut(url=absolute_url)
    # Aksi halde (ör. Firebase Storage) dönen URL mutlak olmalı
    # Relatif ise base_url ile birleştir
    if download_url.startswith("/") and request is not None:
        download_url = f"{str(request.base_url).rstrip('/')}{download_url}"
    return PreviewLinkOut(url=download_url)

@router.get("/{file_id}/download")
def download_file_content(
    file_id: str,
    current_user: UserInDB = Depends(get_current_user_from_query_or_header),
    db: BaseRepository = Depends(get_db_repository),
    storage: BaseStorageAdapter = Depends(get_storage_adapter)
):
    """Dosyayı stream ederek indirir/görüntüler (lokal depolama için)."""
    file_record = db.get_file_by_id(tenant_id=current_user.tenant_id, file_id=file_id)
    if not file_record:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı veya bu firmaya ait değil.")

    # İzin kontrolü (preview ile aynı mantık)
    has_permission = False
    if current_user.role == "Admin" or file_record.owner_id == current_user.id:
        has_permission = True
    else:
        user_role = db.get_role_by_name(tenant_id=current_user.tenant_id, role_name=current_user.role)
        if user_role:
            if (file_record.id in set(user_role.allowed_files) or
                (file_record.folder_id and file_record.folder_id in set(user_role.allowed_folders))):
                has_permission = True
    if not has_permission:
        raise HTTPException(status_code=403, detail="Bu dosyayı görüntüleme yetkiniz yok.")

    # Eğer dosya external storage'dan geliyorsa, Google Drive/OneDrive'dan indir
    if file_record.external_file_id and file_record.external_storage_type:
        try:
            content_bytes = _download_from_external_storage(file_record, current_user)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"External storage'dan dosya indirilemedi: {str(e)}")
    else:
        # Normal dosyalar için mevcut mantık
        if not file_record.storage_path:
            raise HTTPException(status_code=404, detail="Dosya storage path'i bulunamadı.")
        try:
            content_bytes = storage.download_file_content(storage_path=file_record.storage_path)
        except Exception:
            raise HTTPException(status_code=404, detail="Dosya içeriği okunamadı.")

    media_type, _ = mimetypes.guess_type(file_record.name)
    media_type = media_type or "application/octet-stream"
    return StreamingResponse(io.BytesIO(content_bytes), media_type=media_type, headers={
        "Content-Disposition": _build_content_disposition(file_record.name, disposition="inline")
    })

# --- DÜZELTİLMİŞ ENDPOINT ---
# Bu fonksiyonun girintisi (indentation) düzeltildi.
@router.put("/{file_id}/rename")
def rename_file_endpoint(
    file_id: str,
    rename_data: FileRename,
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """Bir dosyanın adını günceller. (Admin veya dosya sahibi olmalı)"""
    return file_service.rename_file_service(
        user=current_user,
        file_id=file_id,
        rename_data=rename_data,
        db=db
    )

@router.get("/{file_id}/pdf-preview")
def office_pdf_preview(
    file_id: str,
    current_user: UserInDB = Depends(get_current_user_from_query_or_header),
    db: BaseRepository = Depends(get_db_repository),
    storage: BaseStorageAdapter = Depends(get_storage_adapter)
):
    file_record = db.get_file_by_id(tenant_id=current_user.tenant_id, file_id=file_id)
    if not file_record:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı veya bu firmaya ait değil.")
    
    allowed_exts = {"docx", "xlsx", "pptx", "doc", "xls", "ppt"}
    ext = (file_record.name.rsplit(".", 1)[-1] or "").lower()
    if ext not in allowed_exts:
        raise HTTPException(status_code=415, detail="Bu dosya türü PDF'e dönüştürülerek görüntülenemiyor.")

    has_permission = False
    if current_user.role == "Admin":
        has_permission = True
    elif file_record.owner_id == current_user.id:
        has_permission = True
    else:
        user_role = db.get_role_by_name(tenant_id=current_user.tenant_id, role_name=current_user.role)
        if user_role:
            allowed_file_ids = set(user_role.allowed_files)
            allowed_folder_ids = set(user_role.allowed_folders)
            if (file_record.id in allowed_file_ids or
                (file_record.folder_id and file_record.folder_id in allowed_folder_ids)):
                has_permission = True
    if not has_permission:
        raise HTTPException(status_code=403, detail="Önizleme yetkiniz yok.")

    with tempfile.TemporaryDirectory() as tmpdir:
        original_name = Path(file_record.name).name
        safe_name = original_name.replace("/", "_").replace("\\", "_") or f"file_{file_record.id}"
        local_path = os.path.join(tmpdir, safe_name)
        
        # Eğer dosya external storage'dan geliyorsa, Google Drive/OneDrive'dan indir
        if file_record.external_file_id and file_record.external_storage_type:
            try:
                file_content = _download_from_external_storage(file_record, current_user)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"External storage'dan dosya indirilemedi: {str(e)}")
        else:
            # Normal dosyalar için mevcut mantık
            if not file_record.storage_path:
                raise HTTPException(status_code=404, detail="Dosya storage path'i bulunamadı.")
            file_content = storage.download_file_content(file_record.storage_path)
        
        with open(local_path, "wb") as f:
            f.write(file_content)
        # Libreoffice ile PDF'e dönüştür
        try:
            subprocess.run([
                "libreoffice", "--headless", "--convert-to", "pdf", local_path, "--outdir", tmpdir
            ], check=True)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Libreoffice ile PDF'e dönüştürme başarısız: {e}")
        pdf_name = Path(safe_name).stem + ".pdf"
        pdf_path = os.path.join(tmpdir, pdf_name)
        if not os.path.exists(pdf_path):
            # Libreoffice bazen dosya adını değiştirebilir
            for f in os.listdir(tmpdir):
                if f.lower().endswith(".pdf"):
                    pdf_path = os.path.join(tmpdir, f)
                    break
            else:
                raise HTTPException(status_code=500, detail="PDF dosyası oluşturulamadı.")
        return FileResponse(pdf_path, media_type="application/pdf")

@router.get("/{file_id}/preview")
def preview_file_content(
    file_id: str,
    current_user: UserInDB = Depends(get_current_user_from_query_or_header),
    db: BaseRepository = Depends(get_db_repository),
    storage: BaseStorageAdapter = Depends(get_storage_adapter)
):
    """Dosyayı inline olarak ön izleme için stream eder (PDF ve diğerleri için)."""
    file_record = db.get_file_by_id(tenant_id=current_user.tenant_id, file_id=file_id)
    if not file_record:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı veya bu firmaya ait değil.")

    # İzin kontrolü (download ile aynı mantık)
    has_permission = False
    if current_user.role == "Admin" or file_record.owner_id == current_user.id:
        has_permission = True
    else:
        user_role = db.get_role_by_name(tenant_id=current_user.tenant_id, role_name=current_user.role)
        if user_role:
            if (file_record.id in set(user_role.allowed_files) or
                (file_record.folder_id and file_record.folder_id in set(user_role.allowed_folders))):
                has_permission = True
    if not has_permission:
        raise HTTPException(status_code=403, detail="Bu dosyayı görüntüleme yetkiniz yok.")

    # Eğer dosya external storage'dan geliyorsa, Google Drive/OneDrive'dan indir
    if file_record.external_file_id and file_record.external_storage_type:
        try:
            content_bytes = _download_from_external_storage(file_record, current_user)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"External storage'dan dosya indirilemedi: {str(e)}")
    else:
        # Normal dosyalar için mevcut mantık
        if not file_record.storage_path:
            raise HTTPException(status_code=404, detail="Dosya storage path'i bulunamadı.")
        try:
            content_bytes = storage.download_file_content(storage_path=file_record.storage_path)
        except Exception:
            raise HTTPException(status_code=404, detail="Dosya içeriği okunamadı.")

    media_type, _ = mimetypes.guess_type(file_record.name)
    media_type = media_type or "application/octet-stream"

    # Cache başlıkları ekle (CORS middleware tarafından yönetiliyor)
    headers = {
        "Content-Disposition": _build_content_disposition(file_record.name, disposition="inline"),
        "Cache-Control": "private, max-age=3600",  # 1 saat cache
        "Accept-Ranges": "bytes",
    }

    return StreamingResponse(
        io.BytesIO(content_bytes),
        media_type=media_type,
        headers=headers
    )