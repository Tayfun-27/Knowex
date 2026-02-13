# backend/app/main.py

import sys
import os
import asyncio # <-- Asenkron işlemler için gerekli
print("\n>>> app/main.py dosyası YÜKLENDİ. <<<")
print(f">>> Python versiyonu: {sys.version} <<<")

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# --- EKSİK OLAN IMPORT BURAYA EKLENDİ ---
from app.services import vector_service 
# ----------------------------------------

# Oluşturduğumuz router'ları import et
from app.api.v1 import auth as auth_router_v1
from app.api.v1 import users as users_router_v1
from app.api.v1 import folders as folders_router_v1
from app.api.v1 import files as files_router_v1
from app.api.v1 import chat as chat_router_v1
from app.api.v1 import tenants as tenants_router_v1
from app.api.v1 import roles as roles_router_v1
from app.api.v1 import settings as settings_router_v1
from app.api.v1 import mail as mail_router_v1
from app.api.v1 import databases as databases_router_v1
from app.api.v1 import user_storage as user_storage_router_v1
from app.api.v1 import integrations as integrations_router_v1


app = FastAPI(
    title="Kurumsal Hafıza Platformu API",
    version="0.1.0",
    description="React (Frontend) ve Python (Backend) ile kurumsal hafıza."
)

# --- Rate Limiting ---
# Limiter'ı app state'e ekle, böylece tüm router'larda kullanılabilir
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.on_event("startup")
async def startup_event():
    """
    Uygulama başladığında çalışır.
    Modeli arka planda yükleyerek kullanıcının ilk işlemde beklemesini önler.
    """
    print(">>> Uygulama başlatılıyor, arka plan işlemleri tetikleniyor... <<<")
    # asyncio.to_thread kullanarak ana döngüyü (main loop) bloklamadan 
    # ayrı bir thread'de çalıştırma yapıyoruz.
    asyncio.create_task(asyncio.to_thread(vector_service.warmup_model_in_background))

# --- CORS Ayarları ---
# Frontend'inizin çalıştığı adreslerin listesi
origins = [
    "http://localhost:5173",
    "http://localhost:5174",
    "https://tproje-32ef0.web.app",
    "https://tproje-32ef0.firebaseapp.com",
    "https://kurumsal-hafiza-api2-1009244212286.europe-west1.run.app",
    "https://app.knowvex.com",
]

extra_origins = os.environ.get("CORS_EXTRA_ORIGINS")
if extra_origins:
    origins.extend([origin.strip() for origin in extra_origins.split(",") if origin.strip()])

# --- Security Headers Middleware ---
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Güvenlik header'larını tüm response'lara ekler."""
    response = await call_next(request)
    
    # X-Frame-Options: Clickjacking koruması
    response.headers["X-Frame-Options"] = "DENY"
    
    # X-Content-Type-Options: MIME type sniffing koruması
    response.headers["X-Content-Type-Options"] = "nosniff"
    
    # X-XSS-Protection: XSS koruması (eski tarayıcılar için)
    response.headers["X-XSS-Protection"] = "1; mode=block"
    
    # Strict-Transport-Security: HTTPS zorunluluğu (production'da)
    if os.environ.get("ENVIRONMENT") == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    
    # Content-Security-Policy: XSS ve injection koruması
    # Not: Frontend'in çalışması için gerekli ayarlamalar yapılabilir
    csp = "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' data:; connect-src 'self' https:;"
    response.headers["Content-Security-Policy"] = csp
    
    # Referrer-Policy: Referrer bilgisi kontrolü
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    
    # Permissions-Policy: Tarayıcı özelliklerini kontrol et
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    
    return response

# Google Cloud Run için CORS ayarları
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "HEAD"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With", "Accept", "Origin"],
    expose_headers=["Content-Length", "Content-Type", "Content-Disposition"],
    max_age=3600,
)

# --- API Router'larını Yükle ---
print(">>> app/main.py: Router'lar yükleniyor... <<<")
app.include_router(
    auth_router_v1.router,
    prefix="/api/v1/auth",
    tags=["V1 - Authentication"]
)
app.include_router(
    users_router_v1.router,
    prefix="/api/v1/users",
    tags=["V1 - Users"]
)
app.include_router(
    folders_router_v1.router,
    prefix="/api/v1/folders",
    tags=["V1 - Folders"]
)
app.include_router(
    files_router_v1.router,
    prefix="/api/v1/files",
    tags=["V1 - Files"]
)
app.include_router(
    chat_router_v1.router,
    prefix="/api/v1/chat",
    tags=["V1 - Chat"]
)
app.include_router(
    tenants_router_v1.router,
    prefix="/api/v1/tenants",
    tags=["V1 - Tenants"]
)
app.include_router(
    roles_router_v1.router,
    prefix="/api/v1/roles",
    tags=["V1 - Roles"]
)
app.include_router(
    settings_router_v1.router,
    prefix="/api/v1/settings",
    tags=["V1 - Settings"]
)
app.include_router(
    mail_router_v1.router,
    prefix="/api/v1/mail",
    tags=["V1 - Mail"]
)
app.include_router(
    databases_router_v1.router,
    prefix="/api/v1/databases",
    tags=["V1 - Databases"]
)
app.include_router(
    user_storage_router_v1.router,
    prefix="/api/v1/user",
    tags=["V1 - User Storage"]
)
app.include_router(
    integrations_router_v1.router,
    prefix="/api/v1/integrations",
    tags=["V1 - API Integrations"]
)

@app.get("/", tags=["Health Check"])
def read_root():
    """API'nin kök dizini. Çalışıp çalışmadığını kontrol eder."""
    return {"status": "ok", "message": "Kurumsal Hafıza API'si çalışıyor!"}