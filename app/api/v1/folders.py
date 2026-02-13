# backend/app/api/v1/folders.py
# (DELETE endpoint eklendi)

from fastapi import APIRouter, Depends, Query, status # <-- status eklendi
from typing import List, Optional
from app.schemas.folder import FolderCreate, FolderOut
from app.schemas.user import UserInDB
from app.services import folder_service
from app.dependencies import get_current_user, get_db_repository, get_current_admin_user, get_storage_adapter # <-- admin dependency ve storage eklendi
from app.repositories.base import BaseRepository
from app.storage_adapters.base import BaseStorageAdapter # <-- storage eklendi

router = APIRouter()

@router.post("/", response_model=FolderOut, status_code=201)
def create_folder(
    folder_data: FolderCreate,
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """
    Giriş yapmış kullanıcı için yeni bir klasör oluşturur.
    
    - **name**: Klasör adı (zorunlu)
    - **parent_id**: (Opsiyonel) Bu klasörün içinde yer alacağı 
      üst klasörün ID'si. Gönderilmezse kök dizine oluşturulur.
    """
    return folder_service.create_new_folder(folder_data, current_user, db)


@router.get("/", response_model=List[FolderOut])
def list_folders(
    parent_id: Optional[str] = Query(None), # Query parametresi olarak al
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """
    Giriş yapmış kullanıcının klasörlerini listeler.
    
    - **parent_id** gönderilmezse (veya null), kök dizindeki klasörleri getirir.
    - **parent_id** gönderilirse, o klasörün içindekileri getirir.
    """
    return folder_service.get_folders_by_owner(current_user, parent_id, db)

# --- YENİ EKLENDİ: Klasör Silme Endpoint'i ---
@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_folder(
    folder_id: str,
    admin_user: UserInDB = Depends(get_current_admin_user), # Sadece Admin erişebilir
    db: BaseRepository = Depends(get_db_repository),
    storage: BaseStorageAdapter = Depends(get_storage_adapter)
):
    """
    (Admin) Bir klasörü ve içindeki tüm içeriği kalıcı olarak siler.
    """
    folder_service.delete_folder_service(
        user=admin_user,
        folder_id=folder_id,
        db=db,
        storage=storage
    )
    return
# --- BİTTİ ---

# --- YENİ EKLENDİ: Klasör Altındaki Toplam Dosya Sayısı ---
@router.get("/{folder_id}/file-count")
def get_folder_file_count(
    folder_id: str,
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """
    Bir klasörün altındaki (tüm alt klasörler dahil) toplam dosya sayısını döndürür.
    """
    file_ids = db.get_all_file_ids_in_folder_recursive(
        tenant_id=current_user.tenant_id,
        folder_id=folder_id,
        user=current_user
    )
    return {"folder_id": folder_id, "file_count": len(file_ids)}
# --- BİTTİ ---