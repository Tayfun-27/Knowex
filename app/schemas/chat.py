# backend/app/schemas/chat.py

from pydantic import BaseModel, Field
from typing import Optional, List, Literal, Dict, Any
from datetime import datetime


# --- Mesaj ve Oturum Modelleri ---
class ChatMessage(BaseModel):
    """Tek bir sohbet mesajını temsil eder."""
    sender: Literal["user", "ai", "system"]
    text: str
    created_at: datetime = Field(default_factory=datetime.now)
    metadata: Optional[Dict[str, Any]] = None

class ChatSession(BaseModel):
    """Bir sohbet oturumunu temsil eder."""
    id: str
    user_id: str
    tenant_id: str
    title: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)

# --- Aktif Bağlam Dosyası Modeli ---
class ActiveContextFile(BaseModel):
    """Frontend ve Backend arasında taşınan aktif bağlam nesnesi."""
    id: str
    name: str
    type: Literal["file", "folder", "database"]
    db_type: Optional[str] = None  # Veritabanı için tip bilgisi

# --- İstek ve Yanıt Modelleri ---

# Kullanıcının frontend'den göndereceği model
class ChatRequest(BaseModel):
    message: str
    chat_id: Optional[str] = None
    model_name: Literal["gemini", "gpt-4o", "claude", "llama"] = "gemini"
    context_files: Optional[List[ActiveContextFile]] = None
    agent_type: Optional[Literal["default", "excel", "presentation"]] = "default"

# API'nin frontend'e göndereceği model
class ChatResponse(BaseModel):
    response_message: str
    source_context: Optional[str] = None
    chat_id: str
    active_context_files: List[ActiveContextFile] = []

    # --- YENİ EKLENEN ALANLAR ---
    # Bu alan, frontend'e gelen yanıtın bir cevap mı yoksa bir öneri mi olduğunu söyler.
    response_type: Literal["answer", "suggestion"] = "answer"
    # Eğer response_type 'suggestion' ise, bu alan önerilen dosyayı içerir.
    suggested_file: Optional[ActiveContextFile] = None
    # AI yanıtı için metrikler
    response_metadata: Optional[Dict[str, Any]] = None

# --- Token Kullanım İstatistikleri ---
class TokenUsageStats(BaseModel):
    """Token kullanım istatistikleri."""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    total_cost_tl: float = 0.0
    message_count: int = 0
    date: str