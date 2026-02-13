# backend/app/dependencies.py
# (get_current_admin_user eklendi)

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from app.core.config import DEPLOYMENT_TYPE
from app.repositories.base import BaseRepository
from app.repositories.firestore_repo import FirestoreRepository
# from app.repositories.postgres_repo import PostgresRepository 

# --- YENİ DEPOLAMA (STORAGE) IMPRT'LARI ---
from app.storage_adapters.base import BaseStorageAdapter
from app.storage_adapters.firebase_storage import FirebaseStorageAdapter
from app.storage_adapters.local_storage import LocalStorageAdapter
# --- BİTTİ ---

from app.schemas.user import UserInDB, TokenData
from app.core.security import decode_access_token
from typing import Optional
from fastapi import Request

# --- Veritabanı Bağımlılığı (Aynen kalıyor) ---
_db_repository = None

def get_db_repository() -> BaseRepository:
    """Yapılandırmaya göre doğru DB repository'sini döndürür."""
    global _db_repository
    if _db_repository is None:
        if DEPLOYMENT_TYPE == "firestore":
            _db_repository = FirestoreRepository()
        elif DEPLOYMENT_TYPE == "postgres":
            raise NotImplementedError("Postgres repository henüz implemente edilmedi.")
        else:
            raise ValueError(f"Bilinmeyen DEPLOYMENT_TYPE: {DEPLOYMENT_TYPE}")
    return _db_repository


# --- YENİ: DEPOLAMA (STORAGE) BAĞIMLILIĞI ---
_storage_adapter = None

def get_storage_adapter() -> BaseStorageAdapter:
    """Yapılandırmaya göre doğru Depolama Adaptörünü döndürür."""
    global _storage_adapter
    if _storage_adapter is None:
        if DEPLOYMENT_TYPE == "firestore":
            _storage_adapter = FirebaseStorageAdapter()
        elif DEPLOYMENT_TYPE == "postgres":
            # Lokal (postgres) kurulumun lokal depolama kullanacağını varsayıyoruz
            _storage_adapter = LocalStorageAdapter()
        else:
            raise ValueError(f"Bilinmeyen DEPLOYMENT_TYPE: {DEPLOYMENT_TYPE}")
    return _storage_adapter
# --- YENİ BİTTİ ---


# --- Güvenlik Bağımlılıkları (Aynen kalıyor) ---
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

async def get_current_user(
    token: str = Depends(oauth2_scheme), 
    db: BaseRepository = Depends(get_db_repository)
) -> UserInDB:
    token_data: TokenData = decode_access_token(token)
    user = db.get_user_by_email(email=token_data.email)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kullanıcı bulunamadı",
        )
    return user

def _resolve_token_from_request(request: Request, token_query: Optional[str]) -> Optional[str]:
    if token_query:
        return token_query
    auth_header = request.headers.get('Authorization') or request.headers.get('authorization')
    if auth_header and auth_header.lower().startswith('bearer '):
        return auth_header.split(' ', 1)[1]
    return None

async def get_current_user_from_query_or_header(
    request: Request,
    token: Optional[str] = None,
    db: BaseRepository = Depends(get_db_repository)
) -> UserInDB:
    raw_token = _resolve_token_from_request(request, token)
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Kimlik doğrulama gerekli")
    token_data: TokenData = decode_access_token(raw_token)
    user = db.get_user_by_email(email=token_data.email)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Kullanıcı bulunamadı")
    return user

# --- YENİ EKLENDİ: Admin Yetki Kontrolü ---
async def get_current_admin_user(
    current_user: UserInDB = Depends(get_current_user_from_query_or_header)
) -> UserInDB:
    """
    Kullanıcının "Admin" rolüne sahip olup olmadığını kontrol eder.
    Değilse, bir yetki hatası (403 Forbidden) fırlatır.
    Çoklu rol desteği: roles listesinde "Admin" varsa yetki verilir.
    """
    # Çoklu rol desteği: roles listesinde "Admin" var mı kontrol et
    user_roles = getattr(current_user, 'roles', None)
    if not user_roles:
        # Geriye dönük uyumluluk: role alanından türet
        user_roles = [current_user.role] if current_user.role else ["User"]
    
    if "Admin" not in user_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bu işlemi yapmak için yetkiniz yok."
        )
    return current_user
# --- YENİ BİTTİ ---