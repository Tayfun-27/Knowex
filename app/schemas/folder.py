# backend/app/schemas/folder.py

from pydantic import BaseModel
from typing import Optional

class FolderBase(BaseModel):
    name: str
    parent_id: Optional[str] = None

class FolderCreate(FolderBase):
    pass

class FolderOut(FolderBase):
    id: str
    owner_id: str
    tenant_id: str  # <-- YENİ: Klasörün hangi firmaya ait olduğu

    class Config:
        from_attributes = True