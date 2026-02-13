# backend/app/core/config.py

import os
from dotenv import load_dotenv

# .env dosyasını yükle
load_dotenv()

# --- GÜVENLİK VE JWT AYARLARI ---
SECRET_KEY = os.environ.get("SECRET_KEY")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 # 1 gün

# --- MİMARİ "SWITCH" AYARLARI ---
DEPLOYMENT_TYPE = os.environ.get("DEPLOYMENT_TYPE", "firestore")

# --- FIRESTORE AYARLARI (Eğer DEPLOYMENT_TYPE = "firestore") ---
GOOGLE_APPLICATION_CREDENTIALS = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")


# --- DEPOLAMA AYARLARI ---

# 'DEPLOYMENT_TYPE' = "postgres" (lokal) ise dosyaların kaydedileceği klasör
LOCAL_STORAGE_PATH = os.environ.get("LOCAL_STORAGE_PATH", "local_uploads")

# 'DEPLOYMENT_TYPE' = "firestore" (bulut) ise kullanılacak Firebase Storage Bucket adı
# Lütfen burayı kendi Firebase projenizdeki bucket adıyla değiştirin
# Genellikle "proje-adi.appspot.com" şeklinde olur.
FIREBASE_STORAGE_BUCKET = os.environ.get("FIREBASE_STORAGE_BUCKET")

# --- YAPAY ZEKA MODEL API AYARLARI ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# --- Lokal LLM (Ollama) için API Adresi ---
# Ollama genellikle http://localhost:11434 adresinde çalışır
# Farklı bir adres kullanmak için environment variable'ı ayarlayın: OLLAMA_API_BASE_URL=http://your-ollama-url:port
OLLAMA_API_BASE_URL = os.environ.get("OLLAMA_API_BASE_URL", "http://localhost:11434")

# --- GÜVENLİK AYARLARI ---
ENVIRONMENT = os.environ.get("ENVIRONMENT", "production")  # development, staging, production
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

# --- HARİCİ DEPOLAMA OAuth AYARLARI ---
# Google Drive OAuth 2.0 Credentials
GOOGLE_DRIVE_CLIENT_ID = os.environ.get("GOOGLE_DRIVE_CLIENT_ID")
GOOGLE_DRIVE_CLIENT_SECRET = os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET")

# OneDrive (Microsoft) OAuth 2.0 Credentials
ONEDRIVE_CLIENT_ID = os.environ.get("ONEDRIVE_CLIENT_ID")
ONEDRIVE_CLIENT_SECRET = os.environ.get("ONEDRIVE_CLIENT_SECRET")