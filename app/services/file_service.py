# backend/app/services/file_service.py

from fastapi import UploadFile, HTTPException, status 
from app.repositories.base import BaseRepository
from app.storage_adapters.base import BaseStorageAdapter
from app.schemas.user import UserInDB
# --- DÜZELTME: FileRename import edildi ---
from app.schemas.file import FileCreate, FileOut, FileRename
from typing import List, Optional
from datetime import datetime
import uuid 
import re
import os
from pathlib import Path
from app.services import vector_service

# --- GÜVENLİK AYARLARI ---
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB limit
ALLOWED_EXTENSIONS = {
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', 
    '.txt', '.md', '.rtf', '.csv',
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.svg', '.webp',
    '.mp4', '.avi', '.mov', '.wmv',
    '.mp3', '.wav', '.ogg',
    '.zip', '.rar', '.7z',
    '.js', '.py', '.java', '.html', '.css', '.json', '.xml'
}
DANGEROUS_EXTENSIONS = {
    '.exe', '.bat', '.cmd', '.com', '.pif', '.scr', '.vbs', '.jar',
    '.sh', '.ps1', '.msi', '.dll', '.sys', '.drv'
}

def sanitize_filename(filename: str) -> str:
    """
    Dosya adını güvenli hale getirir.
    Zararlı karakterleri kaldırır ve path traversal saldırılarını önler.
    """
    # Path traversal karakterlerini kaldır
    filename = filename.replace('..', '').replace('/', '').replace('\\', '')
    
    # Zararlı karakterleri kaldır veya değiştir
    filename = re.sub(r'[<>:"|?*\x00-\x1f]', '', filename)
    
    # Boşlukları temizle ve uzunluk kontrolü
    filename = filename.strip()
    if len(filename) > 255:
        name, ext = os.path.splitext(filename)
        filename = name[:255-len(ext)] + ext
    
    # Boş dosya adı kontrolü
    if not filename or filename == '.' or filename == '..':
        filename = f"file_{uuid.uuid4().hex[:8]}"
    
    return filename

def validate_file_upload(file: UploadFile) -> tuple[str, str]:
    """
    Dosya yüklemesini güvenlik açısından doğrular.
    Returns: (sanitized_filename, file_extension)
    Raises: HTTPException if validation fails
    """
    # 1. Dosya boyutu kontrolü
    if file.size is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Dosya boyutu belirlenemedi."
        )
    
    if file.size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Dosya çok büyük. Maksimum boyut: {MAX_FILE_SIZE / (1024*1024):.0f}MB"
        )
    
    if file.size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Boş dosya yüklenemez."
        )
    
    # 2. Dosya adı sanitizasyonu
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Dosya adı belirtilmedi."
        )
    
    sanitized_filename = sanitize_filename(file.filename)
    
    # 3. Dosya uzantısı kontrolü
    file_path = Path(sanitized_filename)
    file_ext = file_path.suffix.lower()
    
    if not file_ext:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Dosya uzantısı belirtilmedi."
        )
    
    # Tehlikeli uzantı kontrolü
    if file_ext in DANGEROUS_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Bu dosya tipine izin verilmiyor: {file_ext}"
        )
    
    # İzin verilen uzantı kontrolü
    if file_ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Desteklenmeyen dosya tipi: {file_ext}. İzin verilen tipler: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )
    
    return sanitized_filename, file_ext

def upload_new_file(
    file: UploadFile, 
    folder_id: Optional[str], 
    user: UserInDB, 
    db: BaseRepository, 
    storage: BaseStorageAdapter
) -> FileOut:
    """
    Dosya yükleme iş akışını yönetir:
    1. Dosyaya benzersiz bir isim oluştur.
    2. Dosyayı depolama adaptörünü kullanarak yükle.
    3. Dosya kaydını (metadata) veritabanına oluştur.
    4. Dosyayı vektör veritabanına indexle.
    """
    
    # 0. Güvenlik validasyonu
    sanitized_filename, file_ext = validate_file_upload(file)
    
    # Mükerrer Dosya Kontrolü
    if db.check_file_exists(tenant_id=user.tenant_id, folder_id=folder_id, file_name=sanitized_filename):
        raise HTTPException(
            status_code=409, # 409 Conflict, mükerrer kaynak için doğru koddur.
            detail=f"Bu konumda '{sanitized_filename}' adında bir dosya zaten mevcut."
        )
    
    # 1. Benzersiz dosya adı oluştur
    unique_filename = f"{uuid.uuid4()}_{sanitized_filename}"

    # 2. Dosyayı depolama alanına yükle
    storage_path = storage.upload_file(
        file_obj=file.file,
        tenant_id=user.tenant_id,
        file_name=unique_filename
    )
    
    # 3. Dosya bilgilerini veritabanına kaydet
    file_data = FileCreate(
        name=sanitized_filename,  # Sanitize edilmiş dosya adını kullan
        folder_id=folder_id,
        content_type=file.content_type,
        size=file.size,
        owner_id=user.id,
        tenant_id=user.tenant_id,
        storage_path=storage_path,
        created_at=datetime.now()
    )
    
    new_file_record = db.create_file_record(file_data)
    
    # 4. Vektör Indexleme
    try:
        vector_service.index_file(new_file_record, user, db, storage)
    except Exception as e:
        print(f"KRİTİK HATA: Dosya yüklendi (ID: {new_file_record.id}) ancak vektör indexleme başarısız oldu: {e}")
        # TODO: new_file_record'u ve storage'daki dosyayı sil (rollback)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Dosya yüklendi ancak indekslenemedi (Vektör Hatası): {str(e)}"
        )
    
    return new_file_record

def get_files_in_folder(
    user: UserInDB, 
    folder_id: Optional[str], 
    db: BaseRepository
) -> List[FileOut]:
    """
    Kullanıcının rolüne ve sahipliğe göre klasördeki dosyaları getirir.
    """
    if user.role == "Admin":
        return db.get_files_by_folder(tenant_id=user.tenant_id, folder_id=folder_id)

    user_role = db.get_role_by_name(tenant_id=user.tenant_id, role_name=user.role)
    if not user_role:
        allowed_file_ids = set()
        allowed_folder_ids = set()
    else:
        allowed_file_ids = set(user_role.allowed_files)
        allowed_folder_ids = set(user_role.allowed_folders)

    all_files_in_folder = db.get_files_by_folder(tenant_id=user.tenant_id, folder_id=folder_id)
    
    visible_files = []
    for file in all_files_in_folder:
        if (file.owner_id == user.id or 
            file.id in allowed_file_ids or 
            (file.folder_id and file.folder_id in allowed_folder_ids)):
            visible_files.append(file)
            
    return visible_files

def move_file_service(
    user: UserInDB, 
    file_id: str, 
    new_folder_id: Optional[str], 
    db: BaseRepository
) -> bool:
    """Dosya taşıma iş mantığı."""
    success = db.move_file(
        tenant_id=user.tenant_id,
        file_id=file_id,
        new_folder_id=new_folder_id,
        user=user
    )
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dosya bulunamadı veya bu işlemi yapmaya yetkiniz yok."
        )
    
    return True

def delete_file_service(
    user: UserInDB, 
    file_id: str, 
    db: BaseRepository, 
    storage: BaseStorageAdapter
):
    """
    Bir dosyayı ve ona ait tüm verileri (depolama, veritabanı, vektör) siler.
    """
    file_record = db.get_file_by_id(tenant_id=user.tenant_id, file_id=file_id)
    if not file_record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dosya bulunamadı veya bu dosyaya erişim yetkiniz yok."
        )
        
    storage.delete_file(storage_path=file_record.storage_path)
    db.delete_chunks_for_file(tenant_id=user.tenant_id, file_id=file_id)
    db.delete_file_record(tenant_id=user.tenant_id, file_id=file_id)
    
    return

def rename_file_service(user: UserInDB, file_id: str, rename_data: FileRename, db: BaseRepository):
    """Bir dosyayı yeniden adlandırma iş mantığı."""
    file_record = db.get_file_by_id(tenant_id=user.tenant_id, file_id=file_id)
    if not file_record:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı.")

    if user.role != "Admin" and file_record.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Bu dosyayı yeniden adlandırma yetkiniz yok.")
    
    try:
        success = db.rename_file(
            tenant_id=user.tenant_id,
            file_id=file_id,
            new_name=rename_data.new_name
        )
        if not success:
            raise HTTPException(status_code=500, detail="Dosya adı güncellenemedi.")
    except ValueError as e: 
        raise HTTPException(status_code=409, detail=str(e))
    
    return {"message": "Dosya başarıyla yeniden adlandırıldı."}