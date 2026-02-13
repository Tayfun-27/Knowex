# backend/app/repositories/base.py

from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any, Set # <-- YENİ: Set eklendi
from app.schemas.user import UserCreate, UserInDB, UserOut
from app.schemas.folder import FolderCreate, FolderOut
from app.schemas.file import FileCreate, FileOut 
from app.schemas.tenant import TenantOut
from app.schemas.mention import MentionableItem 
from app.schemas.role import RoleCreate, RoleOut, RolePermissionUpdate
from datetime import datetime
from app.schemas.chat import ChatMessage, ChatSession

class BaseRepository(ABC):

    @abstractmethod
    def get_user_by_email(self, email: str) -> Optional[UserInDB]: pass
    
    @abstractmethod
    def create_user(self, user_create: UserCreate, hashed_password: str) -> UserInDB: pass
    
    @abstractmethod
    def get_users_by_tenant(self, tenant_id: str) -> List[UserOut]:
        """Bir tenant'a (şirkete) ait tüm kullanıcıları listeler."""
        pass
    @abstractmethod
    def update_user_role(self, tenant_id: str, user_id: str, new_role: str) -> bool:
        """Kullanıcının rolünü günceller (güvenlik için tenant_id kontrolü ile). Geriye dönük uyumluluk için korundu."""
        pass
    
    @abstractmethod
    def update_user_roles(self, tenant_id: str, user_id: str, new_roles: List[str], new_role: str) -> bool:
        """Kullanıcının rollerini günceller (çoklu rol desteği)."""
        pass
        
    @abstractmethod
    def delete_user(self, tenant_id: str, user_id: str) -> bool:
        """Kullanıcıyı siler (güvenlik için tenant_id kontrolü ile)."""
        pass
    # --- Folder Metodları ---
    @abstractmethod
    def create_folder(self, folder_data: FolderCreate, owner_id: str, tenant_id: str) -> FolderOut: pass

    @abstractmethod
    def get_folders_by_parent(self, tenant_id: str, parent_id: Optional[str]) -> List[FolderOut]: pass   

    @abstractmethod
    def delete_folder(self, tenant_id: str, folder_id: str) -> bool:
        """Bir klasörü veritabanından siler."""
        pass
  
    @abstractmethod
    def create_file_record(self, file_data: FileCreate) -> FileOut:
        """Veritabanına bir dosya kaydı (metadata) ekler."""
        pass
        
    @abstractmethod 
    def get_files_by_folder(self, tenant_id: str, folder_id: Optional[str]) -> List[FileOut]:
        """Belirli bir klasördeki dosyaları listeler."""
        pass

    @abstractmethod
    def get_file_by_id(self, tenant_id: str, file_id: str) -> Optional[FileOut]:
        """
        Belirli bir dosyayı ID'sine göre getirir.
        Güvenlik için 'tenant_id'yi de kontrol etmelidir.
        """
        pass
    
    @abstractmethod
    def delete_file_record(self, tenant_id: str, file_id: str) -> bool:
        """Bir dosyanın kaydını (metadata) veritabanından siler."""
        pass

    @abstractmethod
    def move_file(self, tenant_id: str, file_id: str, new_folder_id: Optional[str], user: UserInDB) -> bool:
        """
        Bir dosyanın 'folder_id'sini (konumunu) günceller.
        Admin olmayan kullanıcılar için SAHİPLİK KONTROLÜ yapmalıdır.
        """
        pass
    
    @abstractmethod
    def get_tenant_by_id(self, tenant_id: str) -> Optional[TenantOut]:
        """Bir tenant'ı (şirketi) ID'sine göre getirir."""
        pass
        
    @abstractmethod
    def get_all_files_for_tenant(self, tenant_id: str) -> List[FileOut]:
        """Bir tenant'a ait tüm dosyaları @ mention listesi için getirir."""
        pass

    @abstractmethod
    def get_all_folders_for_tenant(self, tenant_id: str) -> List[FolderOut]:
        """Bir tenant'a ait tüm klasörleri @ mention listesi için getirir."""
        pass
        
    @abstractmethod
    def get_all_file_ids_in_folder_recursive(self, tenant_id: str, folder_id: str, user: UserInDB) -> List[str]:
        """
        Bir klasör ID'si alır ve o klasörün içindeki ve
        tüm alt klasörlerindeki (recursive) dosyaların ID'lerini,
        KULLANICININ İZİNLERİNE GÖRE FİLTRELENMİŞ olarak
        bir liste halinde döndürür.
        """
        pass
    @abstractmethod
    def add_text_chunks_batch(self, chunks_data: List[Dict[str, Any]]):
        """
        Vektörleri ve metin parçalarını (chunks) 'text_chunks' 
        koleksiyonuna toplu olarak ekler.
        """
        pass

    # --- DEĞİŞİKLİK BURADA: İmza güncellendi ---
    @abstractmethod
    def find_similar_chunks(
        self, 
        tenant_id: str, 
        query_vector: List[float], 
        limit: int,
        file_id_filter: Optional[Set[str]] = None # <-- YENİ EKLENDİ
    ) -> List[Dict[str, Any]]:
        """
        Firestore'da vektör araması yapar ve en yakın 
        'limit' kadar sonucu döndürür.
        Eğer 'file_id_filter' verilirse, arama bu dosya ID'leri ile kısıtlanır.
        """
        pass
    # --- DEĞİŞİKLİK BİTTİ ---

    @abstractmethod
    def delete_chunks_for_file(self, tenant_id: str, file_id: str):
        """
        Bir dosya silindiğinde, o dosyaya ait tüm vektörleri (chunks)
        veritabanından temizler.
        """
        pass
    @abstractmethod
    def get_chat_sessions(self, user_id: str, tenant_id: str) -> List[ChatSession]:
        """Kullanıcının tüm sohbet oturumlarını listeler."""
        pass

    @abstractmethod
    def get_chat_session_by_id(self, chat_id: str, tenant_id: str) -> Optional[ChatSession]:
        """Tek bir sohbet oturumunu ID'sine göre getirir."""
        pass

    @abstractmethod
    def create_chat_session(self, user_id: str, tenant_id: str, title: str) -> ChatSession:
        """Yeni bir sohbet oturumu oluşturur."""
        pass

    @abstractmethod
    def get_chat_messages(self, chat_id: str, tenant_id: str) -> List[ChatMessage]:
        """Bir sohbete ait tüm mesajları getirir."""
        pass

    @abstractmethod
    def get_chat_messages_by_date_range(self, tenant_id: str, user_id: str, 
                                       start_date: datetime, end_date: datetime) -> List[ChatMessage]:
        """Belirli bir tarih aralığındaki tüm chat mesajlarını getirir."""
        pass

    @abstractmethod
    def save_chat_message(self, chat_id: str, tenant_id: str, message: ChatMessage):
        """Bir sohbet oturumuna yeni bir mesaj ekler."""
        pass

    @abstractmethod
    def delete_chat_session(self, chat_id: str, tenant_id: str) -> bool:
        """Bir sohbet oturumunu ve içindeki tüm mesajları siler."""
        pass    
    
    @abstractmethod
    def set_password_reset_token(self, user_id: str, token: str, expires_at: datetime) -> bool:
        """Kullanıcı için şifre sıfırlama token'ı ve son kullanma tarihi belirler."""
        pass
        
    @abstractmethod
    def get_user_by_reset_token(self, token: str) -> Optional[UserInDB]:
        """Geçerli bir token'a sahip kullanıcıyı getirir."""
        pass
        
    @abstractmethod
    def set_user_password(self, user_id: str, hashed_password: str) -> bool:
        """Kullanıcının şifresini günceller ve token'ı siler."""
        pass
    
    @abstractmethod
    def create_role(self, role_data: RoleCreate) -> RoleOut:
        """Yeni bir rol oluşturur."""
        pass
        
    @abstractmethod
    def get_roles_by_tenant(self, tenant_id: str) -> List[RoleOut]:
        """Bir tenant'a ait tüm rolleri listeler."""
        pass
    @abstractmethod
    def update_role_permissions(self, tenant_id: str, role_id: str, permissions: RolePermissionUpdate) -> bool:
        """Bir rolün dosya/klasör izinlerini günceller."""
        pass
    @abstractmethod
    def is_role_assigned_to_users(self, tenant_id: str, role_name: str) -> bool:
        """Bir rolün en az bir kullanıcıya atanıp atanmadığını kontrol eder."""
        pass
        
    @abstractmethod
    def delete_role(self, tenant_id: str, role_id: str) -> bool:
        """Bir rolü (güvenlik kontrolüyle) siler."""
        pass
    @abstractmethod
    def get_role_by_id(self, tenant_id: str, role_id: str) -> Optional[RoleOut]:
        """Bir rolü ID'sine ve tenant'ına göre getirir."""
        pass
    @abstractmethod
    def get_role_by_name(self, tenant_id: str, role_name: str) -> Optional[RoleOut]:
        """Bir rolü ADINA ve tenant'ına göre getirir."""
        pass
    @abstractmethod
    def get_files_by_ids(self, tenant_id: str, file_ids: List[str]) -> List[FileOut]:
        """Verilen ID listesindeki dosyaları getirir."""
        pass

    @abstractmethod
    def get_folders_by_ids(self, tenant_id: str, folder_ids: List[str]) -> List[FolderOut]:
        """Verilen ID listesindeki klasörleri getirir."""
    
    @abstractmethod
    def rename_file(self, tenant_id: str, file_id: str, new_name: str) -> bool:
        """Bir dosyanın adını günceller."""
        pass

    @abstractmethod
    def check_file_exists(self, tenant_id: str, folder_id: Optional[str], file_name: str) -> bool:
        """Belirli bir konumda aynı isimde bir dosya olup olmadığını kontrol eder."""
        pass