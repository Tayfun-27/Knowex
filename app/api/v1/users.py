# backend/app/api/v1/users.py
# (Kullanıcı listeleme ve davet etme eklendi)

from fastapi import APIRouter, Depends, HTTPException, status, Request
from app.schemas.user import ( # Import'lar güncellendi
    UserOut, UserInDB, UserInvite, UserCreate, UserRoleUpdate
)
from app.dependencies import get_current_user, get_db_repository,get_current_admin_user
from app.services import auth_service # <-- Auth service eklendi
from app.repositories.base import BaseRepository # <-- BaseRepo eklendi
from typing import List # <-- List eklendi
from app.core.config import ENVIRONMENT, DEBUG
from slowapi import Limiter
from slowapi.util import get_remote_address

def safe_error_message(e: Exception, default_message: str) -> str:
    """Production'da hassas bilgi sızıntısını önlemek için güvenli hata mesajı döndürür."""
    if ENVIRONMENT == "production" and not DEBUG:
        return default_message
    else:
        return f"{default_message}: {str(e)}"

limiter = Limiter(key_func=get_remote_address)

router = APIRouter()

@router.get("/me", response_model=UserOut)
def read_users_me(
    current_user: UserInDB = Depends(get_current_user)
):
    """
    O an giriş yapmış kullanıcının bilgilerini döndürür.
    """
    return UserOut.model_validate(current_user)

# --- YENİ EKLENDİ: Kullanıcıları Listele ---
@router.get("/", response_model=List[UserOut])
def read_tenant_users(
    admin_user: UserInDB = Depends(get_current_admin_user), # Değiştirildi
    db: BaseRepository = Depends(get_db_repository)
):
    """(Admin) Kendi firmasındaki (tenant) tüm kullanıcıları listeler."""
    return db.get_users_by_tenant(tenant_id=admin_user.tenant_id)

# --- DEĞİŞİKLİK: Sadece adminler kullanıcı davet edebilir ---
@router.post("/invite", response_model=UserOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")  # Rate limiting: 10 davet/dakika
def invite_new_user(
    request: Request,
    user_invite: UserInvite,
    admin_user: UserInDB = Depends(get_current_admin_user), # Değiştirildi
    db: BaseRepository = Depends(get_db_repository)
):
    """(Admin) Yeni bir kullanıcıyı şifresiz olarak davet eder."""
    user_create_data = user_invite.model_dump(exclude_none=True)
    user_create_data['tenant_id'] = admin_user.tenant_id
    
    # Çoklu rol desteği: roles varsa kullan, yoksa role'den türet
    if user_invite.roles:
        user_create_data['roles'] = user_invite.roles
        if not user_create_data.get('role') or user_create_data.get('role') == "User":
            user_create_data['role'] = user_invite.roles[0] if user_invite.roles else "User"
    elif user_invite.role:
        user_create_data['roles'] = [user_invite.role]
        user_create_data['role'] = user_invite.role
    else:
        user_create_data['roles'] = ["User"]
        user_create_data['role'] = "User"
    
    try:
        user_create = UserCreate(**user_create_data)
        new_user = auth_service.register_user(user_create, db)
        auth_service.generate_password_token_for_user(new_user.email, db)
        return UserOut.model_validate(new_user)
    except HTTPException:
        raise  # HTTPException'ları olduğu gibi fırlat
    except Exception as e:
        # Production'da hassas bilgi sızıntısını önle
        if ENVIRONMENT == "production" and not DEBUG:
            raise HTTPException(status_code=500, detail="Kullanıcı oluşturulurken bir hata oluştu.")
        else:
            raise HTTPException(status_code=500, detail=f"Kullanıcı oluşturulurken bir hata oluştu: {str(e)}")


# --- DEĞİŞİKLİK: Sadece adminler rol güncelleyebilir ---
@router.patch("/{user_id}/role", response_model=UserOut)
def update_user_role_endpoint(
    user_id: str,
    role_update: UserRoleUpdate,
    admin_user: UserInDB = Depends(get_current_admin_user), # Değiştirildi
    db: BaseRepository = Depends(get_db_repository)
):
    """(Admin) Bir kullanıcının rolünü günceller. Çoklu rol desteği var."""
    if user_id == admin_user.id:
        raise HTTPException(status_code=400, detail="Admin kendi rolünü bu yolla değiştiremez.")
    
    # Çoklu rol desteği: roles varsa kullan, yoksa role'den türet
    if role_update.roles:
        new_roles = role_update.roles
        new_role = role_update.roles[0] if role_update.roles else "User"
    elif role_update.role:
        new_roles = [role_update.role]
        new_role = role_update.role
    else:
        raise HTTPException(status_code=400, detail="Rol veya roller belirtilmelidir.")
    
    success = db.update_user_roles(tenant_id=admin_user.tenant_id, user_id=user_id, new_roles=new_roles, new_role=new_role)
    if not success:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı veya yetkiniz yok.")
    # Firestore'dan kullanıcıyı tekrar çekmemiz lazım, bu kısım hatalı olabilir.
    # Şimdilik basitçe güncellenmiş halini döndürelim.
    user_doc = db.users_collection.document(user_id).get()
    if not user_doc.exists:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı.")
    return UserOut(id=user_doc.id, **user_doc.to_dict())


# --- DEĞİŞİKLİK: Sadece adminler kullanıcı silebilir ---
@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user_endpoint(
    user_id: str,
    admin_user: UserInDB = Depends(get_current_admin_user), # Değiştirildi
    db: BaseRepository = Depends(get_db_repository)
):
    """(Admin) Bir kullanıcıyı firmasından (tenant) siler."""
    if user_id == admin_user.id:
        raise HTTPException(status_code=400, detail="Admin kendi kendini silemez.")
    success = db.delete_user(tenant_id=admin_user.tenant_id, user_id=user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı veya silme yetkiniz yok.")
    return