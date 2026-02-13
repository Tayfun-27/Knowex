# backend/app/schemas/role.py
# (İzin listeleri eklendi)

from pydantic import BaseModel
from typing import Optional, List

class RoleBase(BaseModel):
    name: str
    description: Optional[str] = None
    
    # --- YENİ EKLENDİ: İzin Listeleri ---
    allowed_folders: List[str] = [] 
    allowed_files: List[str] = []
    # --- BİTTİ ---

class RoleCreate(RoleBase):
    tenant_id: str

class RoleOut(RoleCreate):
    id: str

    class Config:
        from_attributes = True

# --- YENİ EKLENDİ: İzin Güncelleme Şeması ---
class RolePermissionUpdate(BaseModel):
    allowed_folders: List[str] = []
    allowed_files: List[str] = []
# --- BİTTİ ---