# backend/app/services/folder_service.py

from typing import List, Optional
from app.repositories.base import BaseRepository
from app.schemas.folder import FolderCreate, FolderOut
from app.schemas.user import UserInDB
# --- YENİ IMPORTLAR ---
from fastapi import HTTPException, status
from app.services import file_service
from app.storage_adapters.base import BaseStorageAdapter
# --- BİTTİ ---


def create_new_folder(
    folder_data: FolderCreate, 
    owner: UserInDB, 
    db: BaseRepository
) -> FolderOut:
    """Yeni bir klasör oluşturma iş mantığı."""
    return db.create_folder(
        folder_data=folder_data, 
        owner_id=owner.id, 
        tenant_id=owner.tenant_id  # <-- Kullanıcıdan tenant_id'yi al
    )

# --- DÜZELTME BURADA ---
def get_folders_by_owner(
    owner: UserInDB, 
    parent_id: Optional[str], 
    db: BaseRepository
) -> List[FolderOut]:
    """
    Kullanıcının klasör ağacını getirir.
    
    GÜNCELLEME: İzin sistemi, klasör ağacını gizlemek yerine
    içindeki *dosyaları* filtrelemek üzerine kuruludur.
    (Bkz: file_service.get_files_in_folder)
    
    Bu nedenle, bu fonksiyon (tıpkı Admin gibi) TÜM kullanıcılar için
    ilgili dizindeki tüm klasörleri döndürmelidir.
    Kullanıcılar ağaçta gezinebilmeli, ancak yalnızca izinli oldukları
    dosyaları görebilmelidir.
    """
    
    # 1. Eğer kullanıcı Admin ise, tüm klasörleri filtresiz getir (mevcut davranış)
    # if owner.role == "Admin":
    #     return db.get_folders_by_parent(tenant_id=owner.tenant_id, parent_id=parent_id)
    
    # 2. Admin değilse, rol izinlerini uygula (--- ESKİ HATALI KISIM ---)
    # ... (eski kodun tamamı silindi) ...

    # YENİ (ve Basitleştirilmiş) MANTIK:
    # Rolü ne olursa olsun, o dizindeki tüm klasörleri getir.
    # Güvenlik, dosya listeleme (file_service) ve
    # dosya indirme (files.py endpoint'leri) katmanlarında sağlanmaktadır.
    
    print(f"KULLANICI ({owner.email}, Rol: {owner.role}) için '{parent_id or 'Kök Dizin'}' klasörleri listeleniyor.")
    
    return db.get_folders_by_parent(
        tenant_id=owner.tenant_id, 
        parent_id=parent_id
    )
# --- DÜZELTME BİTTİ ---


def delete_folder_service(
    user: UserInDB, 
    folder_id: str, 
    db: BaseRepository, 
    storage: BaseStorageAdapter
):
    """
    Bir klasörü, içindeki tüm alt klasörleri ve dosyalarıyla birlikte
    özyinelemeli (recursive) olarak siler.
    """
    
    folders_to_process = [folder_id]
    
    while folders_to_process:
        current_folder_id = folders_to_process.pop(0)
        
        # 1. Klasörün varlığını ve yetkisini kontrol et
        if current_folder_id == folder_id:
            # DÜZELTME: Sadece kök dizini değil, tüm klasörleri kontrol et
            all_folders = db.get_all_folders_for_tenant(user.tenant_id)
            if not any(f.id == folder_id for f in all_folders):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Klasör bulunamadı veya erişim yetkiniz yok."
                )

        # 2. Bu klasörün içindeki dosyaları sil
        files_in_folder = db.get_files_by_folder(user.tenant_id, current_folder_id)
        for file in files_in_folder:
            print(f"Alt dosya siliniyor: {file.name} (ID: {file.id})")
            file_service.delete_file_service(user, file.id, db, storage)
            
        # 3. Bu klasörün içindeki alt klasörleri işlem listesine ekle
        subfolders = db.get_folders_by_parent(user.tenant_id, current_folder_id)
        for subfolder in subfolders:
            print(f"İşleme eklenecek alt klasör: {subfolder.name} (ID: {subfolder.id})")
            folders_to_process.append(subfolder.id)
            
        # 4. Mevcut klasörün kaydını veritabanından sil
        print(f"Klasör kaydı siliniyor: {current_folder_id}")
        db.delete_folder(user.tenant_id, current_folder_id)
        
    return