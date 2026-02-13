# backend/app/services/chat_suggestion.py
import logging
from collections import defaultdict
from typing import Dict, Optional

from app.schemas.chat import ChatRequest, ChatResponse, ActiveContextFile
from app.schemas.user import UserInDB
from app.repositories.base import BaseRepository
from app.services import vector_service
from app.services import rag_ranking  # _calculate_filename_match_score için
from app.services import rag_retrievers # create_hypothetical_document için
from app.services.chat_context import ContextMemory
from app.services import chat_context # <-- YENİ IMPORT
from app.core import config as app_config

logger = logging.getLogger(__name__)

# --- Config-backed thresholds ---
FILENAME_MATCH_STRONG = getattr(app_config, 'FILENAME_MATCH_STRONG', 1.9)
VECTOR_SCORE_MIN = getattr(app_config, 'VECTOR_SCORE_MIN', 0.75)
CHAMPION_MIN_CHUNKS = getattr(app_config, 'CHAMPION_MIN_CHUNKS', 4)

def handle_suggestion_flow(
    request: ChatRequest, 
    user: UserInDB, 
    db: BaseRepository, 
    context_memory: ContextMemory,
    chat_id: str
) -> Optional[ChatResponse]:
    """
    Bağlam yoksa (is_general_search) bir dosya önermeye çalışır.
    Öneri bulunursa ChatResponse döndürür, bulunamazsa None döndürür.
    """
    if context_memory.has_context():
        return None  # Zaten bağlam var, öneri akışını atla

    logger.info("Bağlam yok. 'Öneri' için Geliştirilmiş Hızlı Arama yapılıyor...")
    best_match_file = None
    
    # --- DEĞİŞİKLİK 1: Tüm dosyalar yerine sadece izin verilenleri al ---
    # ESKİ: all_files = db.get_all_files_for_tenant(user.tenant_id)
    all_files = chat_context.get_all_accessible_files_for_user(db, user)
    # --- DEĞİŞİKLİK BİTTİ ---

    highest_score = FILENAME_MATCH_STRONG
    
    # 1. Dosya Adı Eşleşmesi
    for file in all_files:
        score = rag_ranking._calculate_filename_match_score(request.message, file.name)
        if score > highest_score:
            highest_score = score
            best_match_file = file
    
    if best_match_file:
        logger.info(f"💡 Güçlü aday (Dosya Adı Eşleşmesi) bulundu: {best_match_file.name}")
    else:
        # 2. Vektör Eşleşmesi
        logger.info("Dosya adıyla tam eşleşme yok. 'Öneri' için YETKİ FİLTRELİ vektör araması yapılıyor...")
        
        # --- DEĞİŞİKLİK 2: Vektör aramasını izin verilen dosya ID'leri ile kısıtla ---
        allowed_file_ids_for_search = {file.id for file in all_files}
        if not allowed_file_ids_for_search:
            logger.info("Kullanıcının erişebileceği dosya yok, öneri bulunamadı.")
            return None # Arama yapacak dosyası yok
        # --- DEĞİŞİKLİK BİTTİ ---

        hyde_query = rag_retrievers.create_hypothetical_document(request.message, None)
        quick_search_chunks = vector_service.search_similar_chunks(
            tenant_id=user.tenant_id, 
            query=hyde_query, 
            db=db, 
            limit=50, 
            filter_file_ids=allowed_file_ids_for_search # <-- GÜNCELLENDİ
        )
        
        if quick_search_chunks:
            file_scores: Dict[str, float] = defaultdict(float)
            file_counts: Dict[str, int] = defaultdict(int)
            for chunk in quick_search_chunks:
                file_id = chunk.get("source_file_id")
                score = chunk.get("similarity_score", 0.0)
                if file_id and score > VECTOR_SCORE_MIN:
                    file_scores[file_id] += score
                    file_counts[file_id] += 1
            
            if not file_scores:
                 logger.info("Alakalı vektör sonucu bulunamadı (skor eşiği).")
            else:
                # ... (Bu kısım (şampiyon belirleme) olduğu gibi kalabilir) ...
                sorted_by_score = sorted(file_scores.items(), key=lambda item: item[1], reverse=True)
                champion_file_id, champion_total_score = sorted_by_score[0]
                champion_chunk_count = file_counts[champion_file_id]
                is_dominant_by_score = False
                
                if len(sorted_by_score) > 1:
                    second_best_score = sorted_by_score[1][1]
                    if champion_total_score > (second_best_score * 1.5):
                        is_dominant_by_score = True
                else:
                    is_dominant_by_score = True
                    
                if champion_chunk_count >= CHAMPION_MIN_CHUNKS and is_dominant_by_score:
                    logger.info(f"💡 Güçlü aday (Vektör) bulundu: {champion_file_id} (Chunk Sayısı: {champion_chunk_count}, Toplam Skor: {champion_total_score:.2f})")
                    best_match_file = db.get_file_by_id(user.tenant_id, champion_file_id)
                else:
                     logger.info(f"Zayıf aday: {champion_file_id} (Chunk: {champion_chunk_count}, Skor: {champion_total_score:.2f}). Dominant değil veya chunk sayısı yetersiz.")
    
    # ... (Öneri döndürme kısmı olduğu gibi kalabilir) ...
    if best_match_file:
        suggested_file_context = ActiveContextFile(id=best_match_file.id, name=best_match_file.name, type="file")
        response_msg = f"Sorunuzun '{best_match_file.name}' dosyasıyla ilgili olduğunu düşünüyorum. Bu dosyayı bağlama ekleyerek devam edeyim mi?"
        return ChatResponse(
            response_message=response_msg,
            source_context=None,
            chat_id=chat_id,
            active_context_files=context_memory.get_context(),
            response_type="suggestion",
            suggested_file=suggested_file_context
        )
    
    logger.info("Otomatik öneri için güçlü aday bulunamadı. Genel RAG araması yapılacak.")
    return None