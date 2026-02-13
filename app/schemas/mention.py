# backend/app/schemas/mention.py

from pydantic import BaseModel
from typing import List, Literal, Optional

MentionableType = Literal["file", "folder", "database", "api_integration"]

class MentionableItem(BaseModel):
    id: str
    name: str
    type: MentionableType
    # Veritabanı için ek bilgiler
    db_type: Optional[str] = None  # "postgresql", "mongodb", "mysql" vb.
    # API entegrasyonu için ek bilgiler
    description: Optional[str] = None  # API entegrasyonu açıklaması
    # Gelecekte eklenebilir: path: str

class MentionResponse(BaseModel):
    items: List[MentionableItem]