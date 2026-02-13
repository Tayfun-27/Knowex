# backend/app/api/v1/roles.py

from fastapi import APIRouter, Depends, HTTPException, status
from typing import List
from app.schemas.role import RoleBase, RoleCreate, RoleOut, RolePermissionUpdate
from app.schemas.user import UserInDB
# --- DEĞİŞİKLİK: get_current_admin_user import edildi ---
from app.dependencies import get_current_user, get_db_repository, get_current_admin_user
from app.repositories.base import BaseRepository

router = APIRouter()
DEFAULT_ROLES = ["Admin", "User"]

@router.post("/", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
def create_new_role(
    role_base: RoleBase, 
    admin_user: UserInDB = Depends(get_current_admin_user), # Değiştirildi
    db: BaseRepository = Depends(get_db_repository)
):
    """(Admin) Kendi firması (tenant) için yeni bir rol oluşturur."""
    # Aynı isimde rol var mı kontrol et
    existing_role = db.get_role_by_name(tenant_id=admin_user.tenant_id, role_name=role_base.name)
    if existing_role:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{role_base.name}' adında bir rol zaten mevcut."
        )
    
    role_data = RoleCreate(**role_base.model_dump(), tenant_id=admin_user.tenant_id)
    new_role = db.create_role(role_data)
    return new_role

@router.get("/", response_model=List[RoleOut])
def list_tenant_roles(
    admin_user: UserInDB = Depends(get_current_admin_user), # Değiştirildi
    db: BaseRepository = Depends(get_db_repository)
):
    """(Admin) Kendi firmasındaki (tenant) tüm rolleri listeler."""
    return db.get_roles_by_tenant(tenant_id=admin_user.tenant_id)

@router.put("/{role_id}/permissions", response_model=RoleOut)
def update_permissions_for_role(
    role_id: str,
    permissions: RolePermissionUpdate,
    admin_user: UserInDB = Depends(get_current_admin_user), # Değiştirildi
    db: BaseRepository = Depends(get_db_repository)
):
    """(Admin) Bir rolün dosya ve klasör erişim izinlerini günceller."""
    db.update_role_permissions(tenant_id=admin_user.tenant_id, role_id=role_id, permissions=permissions)
    updated_role = db.get_role_by_id(tenant_id=admin_user.tenant_id, role_id=role_id)
    if not updated_role:
        raise HTTPException(status_code=404, detail="Rol bulunamadı.")
    return updated_role

@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role_endpoint(
    role_id: str,
    admin_user: UserInDB = Depends(get_current_admin_user), # Değiştirildi
    db: BaseRepository = Depends(get_db_repository)
):
    """(Admin) Bir rolü firmasından (tenant) siler."""
    role = db.get_role_by_id(tenant_id=admin_user.tenant_id, role_id=role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Rol bulunamadı.")
    if role.name in DEFAULT_ROLES:
        raise HTTPException(status_code=400, detail=f"'{role.name}' varsayılan bir roldür ve silinemez.")
    if db.is_role_assigned_to_users(tenant_id=admin_user.tenant_id, role_name=role.name):
        raise HTTPException(status_code=400, detail="Bu rol kullanıcılara atanmış, silinemez.")
    db.delete_role(tenant_id=admin_user.tenant_id, role_id=role_id)
    return