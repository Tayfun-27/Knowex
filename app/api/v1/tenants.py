# backend/app/api/v1/tenants.py
# (Giriş yapan kullanıcının şirket bilgilerini getiren endpoint)

from fastapi import APIRouter, Depends, HTTPException, status
from app.schemas.tenant import TenantOut
from app.schemas.user import UserInDB
from app.dependencies import get_current_user, get_db_repository
from app.repositories.base import BaseRepository

router = APIRouter()

@router.get("/me", response_model=TenantOut)
def read_current_tenant(
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """
    O an giriş yapmış kullanıcının ait olduğu tenant (şirket)
    bilgilerini döndürür.
    """
    if not current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kullanıcıya atanmış bir tenant bulunamadı.",
        )
        
    tenant = db.get_tenant_by_id(tenant_id=current_user.tenant_id)
    
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant (şirket) bilgileri (ID: {current_user.tenant_id}) bulunamadı.",
        )
        
    return tenant