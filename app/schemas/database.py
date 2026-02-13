# backend/app/schemas/database.py

from pydantic import BaseModel
from typing import Literal, Optional, Dict, Any

DatabaseType = Literal["postgresql", "mongodb", "mysql","mssql", "sqlite"]

class DatabaseConnection(BaseModel):
    id: Optional[str] = None
    name: str  # "Müşteri Veritabanı", "Satış DB" vb.
    type: DatabaseType
    connection_string: str
    description: Optional[str] = None
    tenant_id: str
    created_by: str
    is_active: bool = True
    created_at: Optional[str] = None

class DatabaseConnectionCreate(BaseModel):
    name: str
    type: DatabaseType
    connection_string: str
    description: Optional[str] = None

class DatabaseConnectionUpdate(BaseModel):
    name: Optional[str] = None
    connection_string: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None

class DatabaseConnectionTest(BaseModel):
    success: bool
    message: str
    schema_preview: Optional[Dict[str, Any]] = None

