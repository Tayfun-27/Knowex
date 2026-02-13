# backend/app/schemas/file.py
# (Toplam ~25 satır)

from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class FileBase(BaseModel):
    name: str
    folder_id: Optional[str] = None # Kök dizindeki dosyalar için null
    content_type: Optional[str] = None
    size: Optional[int] = None

class FileCreate(FileBase):
    """Dosya oluşturma (iç) modeli."""
    owner_id: str
    tenant_id: str
    storage_path: Optional[str] = None  # Artık optional - external storage dosyaları için None olabilir
    external_file_id: Optional[str] = None  # Google Drive/OneDrive file ID
    external_storage_type: Optional[str] = None  # "google_drive" veya "onedrive"
    created_at: datetime = datetime.now()

class FileOut(FileCreate):
    """API'den kullanıcıya döndürülecek dosya modeli."""
    id: str # Firestore Document ID
    
    class Config:
        from_attributes = True
class FileMove(BaseModel):
    """Dosya taşıma isteği için model."""
    new_folder_id: Optional[str] = None # Eğer null ise Kök Dizin'e taşır
    
class FileRename(BaseModel):
    """Dosya yeniden adlandırma isteği için model."""
    new_name: str