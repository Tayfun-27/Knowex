# backend/app/schemas/tenant.py
from pydantic import BaseModel
from typing import Optional

class TenantBase(BaseModel):
    name: str
    status: Optional[str] = None

class TenantOut(TenantBase):
    id: str

    class Config:
        from_attributes = True