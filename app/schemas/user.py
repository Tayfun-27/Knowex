# backend/app/schemas/user.py
# (Şifre alanları opsiyonel hale getirildi, token alanları eklendi, çoklu rol desteği eklendi)

from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, List
from datetime import datetime # <-- YENİ IMPORT

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    email: Optional[EmailStr] = None

class UserBase(BaseModel):
    email: EmailStr
    full_name: Optional[str] = None
    role: str = "User"  # Geriye dönük uyumluluk için korundu
    roles: List[str] = ["User"]  # Yeni çoklu rol desteği
    tenant_id: str
    
    @field_validator('roles', mode='before')
    @classmethod
    def ensure_roles_list(cls, v):
        """Eğer roles yoksa veya boşsa, role'den türet veya varsayılan değer kullan"""
        if v is None:
            return ["User"]
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            return v if v else ["User"]
        return ["User"]
    
    def model_post_init(self, __context):
        """role ve roles arasında senkronizasyon sağla"""
        # Eğer roles boşsa veya sadece role varsa, roles'i role'den türet
        if not self.roles or self.roles == ["User"]:
            if self.role and self.role != "User":
                self.roles = [self.role]
        # Eğer roles varsa ama role yoksa, role'i roles'in ilk elemanı yap
        if self.roles and len(self.roles) > 0 and (not self.role or self.role == "User"):
            self.role = self.roles[0]

class UserCreate(UserBase):
    password: Optional[str] = None # <-- GÜNCELLENDİ (str -> Optional[str])

class UserInDB(UserBase):
    id: str
    hashed_password: Optional[str] = None # <-- GÜNCELLENDİ (str -> Optional[str])
    
    # --- YENİ EKLENDİ: Token alanları ---
    password_reset_token: Optional[str] = None
    token_expires_at: Optional[datetime] = None
    # --- BİTTİ ---

class UserOut(UserBase):
    id: str
    class Config:
        from_attributes = True

# --- GÜNCELLENDİ: Kullanıcı Davet Şeması ---
class UserInvite(BaseModel):
    email: EmailStr
    # password: str # <-- SİLİNDİ
    full_name: Optional[str] = None
    role: Optional[str] = None  # Tek rol için (geriye dönük uyumluluk)
    roles: Optional[List[str]] = None  # Çoklu rol için

# --- BİTTİ ---

# --- YENİ EKLENDİ: Şifre Belirleme İsteği ---
class SetPasswordRequest(BaseModel):
    token: str
    new_password: str
class UserRoleUpdate(BaseModel):
    role: Optional[str] = None  # Tek rol için (geriye dönük uyumluluk)
    roles: Optional[List[str]] = None  # Çoklu rol için
    
class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str