# backend/app/schemas/integration.py

from pydantic import BaseModel, HttpUrl, Field
from typing import Optional, Dict, Any, List
from datetime import datetime
from enum import Enum

class AuthType(str, Enum):
    """API authentication tipi"""
    BASIC = "basic"  # Basic Auth (username/password)
    BEARER = "bearer"  # Bearer Token
    API_KEY = "api_key"  # API Key (header veya query param)
    NONE = "none"  # Authentication yok

class HTTPMethod(str, Enum):
    """HTTP metodları"""
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"

class ApiIntegrationCreate(BaseModel):
    """Yeni API entegrasyonu oluşturma"""
    name: str = Field(..., description="Entegrasyon adı")
    description: Optional[str] = Field(None, description="Açıklama")
    api_url: str = Field(..., description="API base URL (örn: https://api.example.com)")
    auth_type: AuthType = Field(AuthType.BASIC, description="Authentication tipi")
    username: Optional[str] = Field(None, description="Kullanıcı adı (Basic Auth için)")
    password: Optional[str] = Field(None, description="Şifre (Basic Auth için)")
    bearer_token: Optional[str] = Field(None, description="Bearer token (Bearer Auth için)")
    api_key: Optional[str] = Field(None, description="API Key (API Key Auth için)")
    api_key_header: Optional[str] = Field("X-API-Key", description="API Key header adı")
    api_key_location: Optional[str] = Field("header", description="API Key konumu: 'header' veya 'query'")
    custom_headers: Optional[Dict[str, str]] = Field(None, description="Özel header'lar")
    timeout_seconds: Optional[int] = Field(30, description="Request timeout (saniye)")
    is_active: bool = Field(True, description="Entegrasyon aktif mi?")

class ApiIntegrationUpdate(BaseModel):
    """API entegrasyonu güncelleme"""
    name: Optional[str] = None
    description: Optional[str] = None
    api_url: Optional[str] = None
    auth_type: Optional[AuthType] = None
    username: Optional[str] = None
    password: Optional[str] = None  # None ise mevcut değer korunur
    bearer_token: Optional[str] = None  # None ise mevcut değer korunur
    api_key: Optional[str] = None  # None ise mevcut değer korunur
    api_key_header: Optional[str] = None
    api_key_location: Optional[str] = None
    custom_headers: Optional[Dict[str, str]] = None
    timeout_seconds: Optional[int] = None
    is_active: Optional[bool] = None

class ApiIntegration(BaseModel):
    """API entegrasyonu modeli"""
    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    api_url: str
    auth_type: AuthType
    username: Optional[str] = None
    password: Optional[str] = None  # Güvenlik için genelde None döndürülür
    bearer_token: Optional[str] = None  # Güvenlik için genelde None döndürülür
    api_key: Optional[str] = None  # Güvenlik için genelde None döndürülür
    api_key_header: Optional[str] = None
    api_key_location: Optional[str] = None
    custom_headers: Optional[Dict[str, str]] = None
    timeout_seconds: int = 30
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_test_at: Optional[datetime] = None
    last_test_status: Optional[str] = None  # "success", "error", None

class ApiIntegrationResponse(BaseModel):
    """API entegrasyonu response modeli (şifreler gizli)"""
    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    api_url: str
    auth_type: AuthType
    username: Optional[str] = None
    # Şifreler ve token'lar gizli
    has_password: bool = Field(False, description="Şifre kayıtlı mı?")
    has_bearer_token: bool = Field(False, description="Bearer token kayıtlı mı?")
    has_api_key: bool = Field(False, description="API key kayıtlı mı?")
    api_key_header: Optional[str] = None
    api_key_location: Optional[str] = None
    custom_headers: Optional[Dict[str, str]] = None
    timeout_seconds: int = 30
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_test_at: Optional[datetime] = None
    last_test_status: Optional[str] = None

class ApiCallRequest(BaseModel):
    """API çağrısı yapma request modeli"""
    integration_id: str = Field(..., description="Entegrasyon ID")
    endpoint: str = Field(..., description="API endpoint (örn: /users veya /api/v1/users)")
    method: HTTPMethod = Field(HTTPMethod.GET, description="HTTP metodu")
    headers: Optional[Dict[str, str]] = Field(None, description="Ek header'lar")
    params: Optional[Dict[str, Any]] = Field(None, description="Query parametreleri")
    body: Optional[Dict[str, Any]] = Field(None, description="Request body (POST/PUT/PATCH için)")
    timeout: Optional[int] = Field(None, description="Timeout (saniye, varsayılan: entegrasyon ayarı)")

class ApiCallResponse(BaseModel):
    """API çağrısı response modeli"""
    success: bool
    status_code: Optional[int] = None
    headers: Optional[Dict[str, str]] = None
    data: Optional[Any] = None
    error: Optional[str] = None
    response_time_ms: Optional[float] = None

class ApiTestResponse(BaseModel):
    """API test response modeli"""
    success: bool
    status_code: Optional[int] = None
    message: str
    response_time_ms: Optional[float] = None
    error: Optional[str] = None

