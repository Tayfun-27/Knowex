# backend/app/services/auth_service.py

from fastapi import HTTPException, status
from app.repositories.base import BaseRepository
from app.schemas.user import UserCreate, UserInDB
from app.core.security import get_password_hash, verify_password
import secrets
from datetime import datetime, timedelta, timezone
from app.schemas.role import RoleCreate



def register_user(user_create: UserCreate, db: BaseRepository) -> UserInDB:
    db_user = db.get_user_by_email(user_create.email)
    if db_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bu email adresi zaten kayıtlı."
        )
    hashed_password = None
    if user_create.password:
        hashed_password = get_password_hash(user_create.password)
    
    new_user = db.create_user(user_create, hashed_password)

    # --- YENİ MANTIK BURADA BAŞLIYOR ---
    # Bu tenant için daha önce rol oluşturulmuş mu kontrol et
    existing_roles = db.get_roles_by_tenant(tenant_id=new_user.tenant_id)
    if not existing_roles:
        print(f"'{new_user.tenant_id}' tenant'ı için varsayılan roller oluşturuluyor...")
        
        # 1. Admin Rolünü Oluştur
        admin_role_data = RoleCreate(
            name="Admin",
            description="Tüm yetkilere sahip sistem yöneticisi.",
            tenant_id=new_user.tenant_id
        )
        db.create_role(admin_role_data)
        
        # 2. User Rolünü Oluştur
        user_role_data = RoleCreate(
            name="User",
            description="Standart kullanıcı rolü.",
            tenant_id=new_user.tenant_id
        )
        db.create_role(user_role_data)
        print("Varsayılan roller başarıyla oluşturuldu.")
    # --- YENİ MANTIK BURADA BİTİYOR ---
        
    return new_user

def authenticate_user(email: str, password: str, db: BaseRepository) -> UserInDB:
    """
    Kullanıcıyı doğrular. Şifresiz kullanıcıların girişini engeller.
    """
    unauthorized_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Hatalı email veya şifre.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    db_user = db.get_user_by_email(email)
    if not db_user or not db_user.hashed_password or not verify_password(password, db_user.hashed_password):
        raise unauthorized_exception

        
    return db_user

def generate_password_token_for_user(email: str, db: BaseRepository) -> str:
    user = db.get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı")
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    db.set_password_reset_token(user.id, token, expires_at)
    return token

def reset_password_with_token(token: str, new_password: str, db: BaseRepository) -> UserInDB:
    """Token ile şifreyi doğrular, sıfırlar ve kullanıcıyı döndürür."""
        
    user = db.get_user_by_reset_token(token)
    
    if not user:
    
        raise HTTPException(status_code=400, detail="Geçersiz veya süresi dolmuş token.")
        
  
        
    if not user.token_expires_at or user.token_expires_at < datetime.now(timezone.utc):
        # --- DETAYLI LOGLAMA EKLENDİ ---
        print("!!! ADIM 2 HATA: Token'ın süresi dolmuş (auth_service.py) !!!")
        # --- LOGLAMA BİTTİ ---
        raise HTTPException(status_code=400, detail="Token'ın süresi dolmuş.")
        
    hashed_password = get_password_hash(new_password)
    
    success = db.set_user_password(user.id, hashed_password)
    if not success:
        raise HTTPException(status_code=500, detail="Veritabanında şifre güncellenemedi.")
    
    # --- LOGLAMA BİTTİ ---
    user.hashed_password = hashed_password
    user.password_reset_token = None
    user.token_expires_at = None
    return user
def change_user_password(user: UserInDB, old_password: str, new_password: str, db: BaseRepository):
    """Giriş yapmış kullanıcının şifresini değiştirir."""
    
    # 1. Eski şifreyi doğrula
    if not user.hashed_password:
        # Şifresiz kullanıcılar (Google login vb. varsa) için strateji farklı olabilir, 
        # ancak standart akışta şifre olmalıdır.
        raise HTTPException(status_code=400, detail="Mevcut şifre bulunamadı.")
        
    if not verify_password(old_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Mevcut şifre hatalı.")
    
    # 2. Yeni şifreyi hashle
    new_hashed_password = get_password_hash(new_password)
    
    # 3. Veritabanını güncelle
    success = db.set_user_password(user.id, new_hashed_password)
    if not success:
        raise HTTPException(status_code=500, detail="Şifre güncellenemedi.")
        
    return True
    