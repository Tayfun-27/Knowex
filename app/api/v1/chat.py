# backend/app/api/v1/chat.py

from fastapi import APIRouter, Depends, HTTPException, status, Request
from app.schemas.chat import ChatRequest, ChatResponse, ChatSession, ChatMessage, TokenUsageStats
from app.schemas.user import UserInDB
from app.dependencies import get_current_user, get_db_repository, get_storage_adapter
from app.repositories.base import BaseRepository
from app.storage_adapters.base import BaseStorageAdapter
from app.services import chat_service
from typing import List, Optional
from datetime import datetime
from app.core.config import ENVIRONMENT, DEBUG
from slowapi import Limiter
from slowapi.util import get_remote_address

def safe_error_message(e: Exception, default_message: str) -> str:
    """Production'da hassas bilgi sızıntısını önlemek için güvenli hata mesajı döndürür."""
    if ENVIRONMENT == "production" and not DEBUG:
        return default_message
    else:
        return f"{default_message}: {str(e)}"

limiter = Limiter(key_func=get_remote_address)

router = APIRouter()

# --- MEVCUT ENDPOINT GÜNCELLENDİ ---
# Artık hem yeni sohbet başlatır hem de mevcut sohbete devam eder.
@router.post("/", response_model=ChatResponse)
@limiter.limit("30/minute")  # Rate limiting: 30 mesaj/dakika
def handle_chat_message(
    request: Request,
    chat_request: ChatRequest,
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository),
    storage: BaseStorageAdapter = Depends(get_storage_adapter)
):
    """
    Kullanıcıdan bir sohbet mesajı alır, RAG uygular, AI modelinden
    bir yanıt döndürür ve konuşmayı kaydeder.
    """
    try:
        print(f"📥 Chat API çağrısı alındı: model={chat_request.model_name}, mesaj={chat_request.message[:50]}...")
        response = chat_service.process_chat_message(
            request=chat_request,
            user=current_user,
            db=db,
            storage=storage
        )
        print(f"📤 Chat API yanıtı döndürülüyor: mesaj={len(response.response_message)} karakter, chat_id={response.chat_id}")
        return response
    except Exception as e:
        print(f"Chat API Hatası: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=safe_error_message(e, "Sohbet yanıtı işlenirken bir hata oluştu")
        )

# --- YENİ EKLENDİ: Sohbet Geçmişi Endpoint'leri ---

@router.get("/sessions", response_model=List[ChatSession])
def get_user_chat_sessions(
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """Giriş yapmış kullanıcının tüm sohbet oturumlarını listeler."""
    return db.get_chat_sessions(user_id=current_user.id, tenant_id=current_user.tenant_id)


@router.get("/{chat_id}/messages", response_model=List[ChatMessage])
def get_messages_for_session(
    chat_id: str,
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """Belirli bir sohbet oturumundaki tüm mesajları getirir."""
    messages = db.get_chat_messages(chat_id=chat_id, tenant_id=current_user.tenant_id)
    if not messages and not db.get_chat_session_by_id(chat_id, current_user.tenant_id):
         raise HTTPException(status_code=404, detail="Sohbet bulunamadı veya yetkiniz yok.")
    return messages


@router.delete("/sessions/all", status_code=status.HTTP_204_NO_CONTENT)
def delete_all_user_chat_sessions(
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """Giriş yapmış kullanıcının TÜM sohbet geçmişini siler."""
    sessions = db.get_chat_sessions(user_id=current_user.id, tenant_id=current_user.tenant_id)
    for session in sessions:
        db.delete_chat_session(chat_id=session.id, tenant_id=current_user.tenant_id)
    return


@router.get("/token-usage", response_model=TokenUsageStats)
def get_token_usage_stats(
    date: Optional[str] = None,  # YYYY-MM-DD formatında, örn: "2024-11-11"
    start_date: Optional[str] = None,  # Tarih aralığı için başlangıç
    end_date: Optional[str] = None,  # Tarih aralığı için bitiş
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """
    Belirli bir tarih veya tarih aralığı için token kullanım istatistiklerini getirir.
    
    Örnekler:
    - /token-usage?date=2024-11-11  (11 Kasım 2024)
    - /token-usage?start_date=2024-11-01&end_date=2024-11-30  (Kasım ayı)
    - /token-usage  (Bugün)
    """
    # Tarih aralığını belirle
    if date:
        # Tek bir tarih belirtilmişse
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d")
            start_datetime = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
            end_datetime = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)
        except ValueError:
            raise HTTPException(status_code=400, detail="Geçersiz tarih formatı. YYYY-MM-DD formatında olmalı.")
    elif start_date and end_date:
        # Tarih aralığı belirtilmişse
        try:
            start_datetime = datetime.strptime(start_date, "%Y-%m-%d")
            end_datetime = datetime.strptime(end_date, "%Y-%m-%d")
            end_datetime = end_datetime.replace(hour=23, minute=59, second=59)
        except ValueError:
            raise HTTPException(status_code=400, detail="Geçersiz tarih formatı. YYYY-MM-DD formatında olmalı.")
    else:
        # Hiçbir tarih belirtilmemişse bugünü kullan
        today = datetime.now()
        start_datetime = datetime(today.year, today.month, today.day, 0, 0, 0)
        end_datetime = datetime(today.year, today.month, today.day, 23, 59, 59)
    
    # Mesajları tarih aralığına göre getir
    messages = db.get_chat_messages_by_date_range(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        start_date=start_datetime,
        end_date=end_datetime
    )
    
    # Token kullanımını hesapla
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost_usd = 0.0
    total_cost_tl = 0.0
    message_count = 0
    
    for message in messages:
        # Sadece AI mesajlarında token bilgisi var
        if message.sender == "ai" and message.metadata:
            token_usage = message.metadata.get("token_usage", {})
            if token_usage:
                total_input_tokens += token_usage.get("input_tokens", 0)
                total_output_tokens += token_usage.get("output_tokens", 0)
                total_cost_usd += token_usage.get("estimated_cost_usd", 0.0)
                total_cost_tl += token_usage.get("estimated_cost_tl", 0.0)
                message_count += 1
    
    total_tokens = total_input_tokens + total_output_tokens
    
    # Tarih string'i oluştur
    if date:
        date_str = date
    elif start_date and end_date:
        date_str = f"{start_date} - {end_date}"
    else:
        date_str = datetime.now().strftime("%Y-%m-%d")
    
    return TokenUsageStats(
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_tokens=total_tokens,
        total_cost_usd=round(total_cost_usd, 4),
        total_cost_tl=round(total_cost_tl, 2),
        message_count=message_count,
        date=date_str
    )


# --- BİTTİ ---