# backend/app/services/chat_helpers.py
# Chat service yardımcı fonksiyonları
# LocalGPT tarzı Cross-Encoder reranking desteği eklendi

import re
import unicodedata
import json
import os
from typing import List, Tuple
from collections import Counter
from functools import lru_cache

from langchain_core.documents import Document
from langchain_core.messages import SystemMessage, HumanMessage

from app.services.prompts import RERANK_SYSTEM_PROMPT, HYDE_SYSTEM_PROMPT
from app.services.llm_providers import get_llm_for_model, get_cheap_llm
from app.services.token_tracking import TokenTracker, extract_token_usage_from_response

# --- Cross-Encoder için ---
try:
    from sentence_transformers import CrossEncoder
    CROSS_ENCODER_AVAILABLE = True
except ImportError:
    CROSS_ENCODER_AVAILABLE = False
    print("⚠️ sentence-transformers paketi yüklü değil. Cross-Encoder reranking devre dışı.")


def normalize_text_for_matching(text: str) -> str:
    """Metni normalleştir (Türkçe karakterleri temizle)."""
    text = text.lower()
    text = text.replace('ı', 'i').replace('ğ', 'g').replace('ü', 'u').replace('ş', 's').replace('ö', 'o').replace('ç', 'c')
    return ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')


def calculate_filename_match_score(query: str, filename: str) -> float:
    """Dosya adı ile sorgu arasındaki eşleşme skorunu hesapla."""
    normalized_query = normalize_text_for_matching(query)
    normalized_filename = normalize_text_for_matching(filename.rsplit('.', 1)[0])
    query_words = set(re.findall(r'\b\w{3,}\b', normalized_query))
    filename_words = set(re.findall(r'\b\w+\b', normalized_filename))
    if not query_words or not filename_words:
        return 0.0
    intersection = query_words.intersection(filename_words)
    match_ratio = len(intersection) / len(query_words)
    if normalized_query in normalized_filename:
        match_ratio = 2.0
    return match_ratio


def identify_and_filter_high_confidence_document(chunks: List[Document], query: str) -> Tuple[List[Document], bool]:
    """Şampiyon belge tespiti yap (bir dosyadan gelen chunk'lar çok fazlaysa odaklan)."""
    if not chunks:
        return [], False
    
    # Her chunk'a hybrid_score ekle
    for chunk in chunks:
        vector_score = chunk.metadata.get('similarity_score', 0.0)
        chunk.metadata['hybrid_score'] = vector_score
    
    # Skorlarına göre sırala
    chunks.sort(key=lambda x: x.metadata.get('hybrid_score', 0.0), reverse=True)
    top_chunks = chunks[:10]
    
    if not top_chunks:
        return chunks, False
    
    # En üstteki chunk'larda hangi dosya daha çok geçiyor?
    top_file_ids_counts = Counter(
        chunk.metadata.get('source_file_id') for chunk in top_chunks if chunk.metadata.get('source_file_id')
    )
    
    if not top_file_ids_counts:
        return chunks, False
    
    most_common_file_id, count = top_file_ids_counts.most_common(1)[0]
    
    # Eğer en üstteki chunk'larda bir dosyadan 5 veya daha fazla chunk varsa, o dosyaya odaklan
    if count >= 5:
        champion_file_name = next(
            (chunk.metadata.get('source_file_name', 'Bilinmiyor') 
             for chunk in top_chunks 
             if chunk.metadata.get('source_file_id') == most_common_file_id), 
            'Bilinmiyor'
        )
        
        # KRİTİK: Dosya adı ile soru arasında uyum kontrolü yap
        # Eğer dosya adı ile soru uyuşmuyorsa, şampiyon belge olarak kabul etme
        filename_match_score = calculate_filename_match_score(query, champion_file_name)
        
        # Dosya adı ile soru arasında yeterli uyum yoksa, şampiyon belge olarak kabul etme
        # Bu, yanlış dosyaların seçilmesini önler (örn: "açık rıza metni" sorusu için "hurda satış prosedürü" seçilmesi)
        if filename_match_score < 0.15:  # Eşik: %15 uyum gerekli
            print(f"⚠️ Şampiyon belge adayı bulundu ama dosya adı ile soru uyuşmuyor (uyum skoru: {filename_match_score:.2f}). Reranking yapılacak.")
            print(f"   Dosya: '{champion_file_name}'")
            print(f"   Soru: '{query[:100]}...'")
            return chunks, False
        
        print(f"🏆 Şampiyon Belge Tespit Edildi! Odak: '{champion_file_name}' (Uyum skoru: {filename_match_score:.2f})")
        champion_chunks = [chunk for chunk in chunks if chunk.metadata.get('source_file_id') == most_common_file_id]
        return champion_chunks, True
    
    print(f"📚 Şampiyon belge bulunamadı. Vektör skoruna göre en iyi {len(chunks)} chunk yeniden sıralanacak.")
    return chunks, False


def is_list_intent(query: str) -> bool:
    """Sorgunun bir liste isteği olup olmadığını kontrol et."""
    q_lower = normalize_text_for_matching(query)
    # Liste soruları için pattern'ler
    patterns = [
        r"liste", r"kimlere", r"kime", r"hangi", r"firmalar", r"musteriler", r"kisiler", r"prosedurler",
        r"isimleri", r"isimler", r"kimler", r"hangi.*isim", r"hangi.*aday", r"hangi.*kisi",
        r"nedir.*isim", r"nedir.*isimler", r"nedir.*isimleri", r"nedir.*kimler"
    ]
    return any(re.search(p, q_lower) for p in patterns)


@lru_cache(maxsize=1)
def get_reranker_model():
    """Cross-Encoder reranker modelini yükle (LocalGPT tarzı)."""
    if not CROSS_ENCODER_AVAILABLE:
        return None
    
    try:
        # LocalGPT'in önerdiği modeller:
        # - 'BAAI/bge-reranker-base' (multilingual, iyi performans)
        # - 'cross-encoder/ms-marco-MiniLM-L-6-v2' (hızlı)
        model_name = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-base")
        print(f"⚡ Cross-Encoder reranker yükleniyor: {model_name}")
        reranker = CrossEncoder(model_name)
        return reranker
    except Exception as e:
        print(f"⚠️ Cross-Encoder yüklenemedi: {e}")
        return None


def rerank_chunks_with_cross_encoder(
    docs: List[Document],
    question: str,
    top_k: int = 20
) -> List[Document]:
    """
    Cross-Encoder ile reranking (LocalGPT tarzı).
    LLM reranking'den daha hızlı ve genellikle daha doğru.
    """
    if not docs:
        return []
    
    reranker = get_reranker_model()
    if not reranker:
        # Fallback: LLM reranking
        print("⚠️ Cross-Encoder yok, LLM reranking kullanılacak...")
        return None  # None döndür, çağıran fonksiyon LLM reranking yapsın
    
    try:
        # Cross-Encoder için input hazırla: [query, document]
        # 512 token limit (performans için)
        pairs = [[question, doc.page_content[:512]] for doc in docs]
        
        # Skorları hesapla (batch processing)
        scores = reranker.predict(pairs, show_progress_bar=False)
        
        # Skorlara göre sırala
        scored_docs = list(zip(docs, scores))
        scored_docs.sort(key=lambda x: x[1], reverse=True)
        
        # En iyi top_k chunk'ı seç
        reranked = [doc for doc, score in scored_docs[:top_k]]
        
        print(f"✅ Cross-Encoder reranking: {len(docs)} → {len(reranked)} chunk (top_k={top_k})")
        
        return reranked
        
    except Exception as e:
        print(f"❌ Cross-Encoder reranking hatası: {e}")
        # Fallback: İlk top_k chunk'ı döndür
        return docs[:top_k]


def rerank_chunks_with_llm_wrapper(docs: List[Document], question: str, model_name: str, token_tracker: TokenTracker = None, is_list_query: bool = False) -> List[Document]:
    """LLM kullanarak chunk'ları yeniden sırala."""
    if not docs:
        return []
    
    # Soruda firma ismi ve belge türü var mı kontrol et
    question_lower = normalize_text_for_matching(question)
    has_company_name = any(word in question_lower for word in ['firma', 'sirket', 'tedarikci', 'musteri', 'supplier', 'vendor', 'company', 'client', 'customer'])
    has_document_type = any(word in question_lower for word in ['teklif', 'sozlesme', 'fatura', 'po', 'purchase order', 'offer', 'invoice', 'contract'])
    
    # Liste soruları için daha fazla chunk işle
    if is_list_query:
        max_chunks = 300
    elif has_company_name and has_document_type:
        max_chunks = 200  # Firma ismi ve belge türü içeren sorular için daha fazla chunk
    elif has_company_name:
        max_chunks = 150  # Sadece firma ismi içeren sorular için
    else:
        max_chunks = 150  # Normal sorular için
    chunks_to_process = docs[:max_chunks]
    numbered_chunks = "".join(f"[[ALINTI {i+1}]]\n{doc.page_content}\n\n" for i, doc in enumerate(chunks_to_process))
    
    # Soruda firma ismi ve belge türü var mı kontrol et (liste olmasa bile)
    question_lower = normalize_text_for_matching(question)
    has_company_name = any(word in question_lower for word in ['firma', 'sirket', 'tedarikci', 'musteri', 'supplier', 'vendor', 'company', 'client', 'customer'])
    has_document_type = any(word in question_lower for word in ['teklif', 'sozlesme', 'fatura', 'po', 'purchase order', 'offer', 'invoice', 'contract'])
    
    # Liste soruları için özel talimat
    if is_list_query:
        is_supplier_query = has_company_name
        
        if is_supplier_query:
            selection_instruction = """
Bu soru bir TEDARİKÇİ/FİRMA LİSTESİ sorusudur (Örn: 'Hangi tedarikçiler', 'Hangi firmalar'). 
GÖREVİN:
1. İçinde SOMUT FİRMA/TEDARİKÇİ İSMİ geçen alıntıları seç (örn: "ArlanX", "Futura Industrial", "HEPPS-Steel", "AGAR HOSE", "BOSABOX", "Omey", "SBSY", "SBYS", "Huasheng", "KordX").
2. Sözleşme, PO (Purchase Order), teklif, fatura gibi belgelerle ilgili TÜM alıntıları seç - HER BİRİNİ kontrol et.
3. Sadece 'KVKK', 'prosedür tanımı', 'talimat' veya 'boş form' içeren genel metinleri ELE (Seçme) - ama içinde firma ismi geçiyorsa MUTLAKA seç.
4. Eksiksiz liste için en az 250-300 alakalı alıntı seçmeye çalış - FIRMA İSMİ GEÇEN TÜM alıntıları seç.
5. TÜM alakalı alıntıları seç - eksik bilgi vermemek için çok geniş bir seçim yap. "Şüpheli" olanları bile seç - daha sonra filtrelenebilir.
6. Bir alıntıda sadece 1 firma ismi bile olsa, onu mutlaka seç.
7. Farklı alıntılarda farklı firmalar olabilir - HEPSİNİ seç.
8. Dosya adlarında firma ismi geçiyorsa o alıntıyı da seç (örn: "XYZ_Purchase_Order.pdf" içeren alıntı).
9. E-posta adreslerinde firma domain'i varsa o alıntıyı da seç (örn: "sales@firma.com" içeren alıntı).
"""
        else:
            # "isimleri nedir", "kimler", "hangi adaylar" gibi sorular için özel talimat
            is_name_list_query = any(word in question_lower for word in ['isimleri', 'isimler', 'kimler', 'hangi.*aday', 'hangi.*kisi', 'nedir.*isim'])
            
            if is_name_list_query:
                selection_instruction = """
Bu soru bir İSİM LİSTESİ sorusudur (Örn: 'isimleri nedir', 'kimler', 'hangi adaylar'). 
GÖREVİN:
1. İçinde SOMUT KİŞİ İSMİ, ADAY İSMİ veya FİRMA İSMİ geçen TÜM alıntıları seç (örn: "Ahmet Yılmaz", "Elif Karadeniz", "Selin Demir", "Can Öztürk").
2. Aday özeti, görüşme özeti, CV, başvuru belgeleri gibi belgelerle ilgili TÜM alıntıları seç - HER BİRİNİ kontrol et.
3. Sadece 'prosedür tanımı', 'talimat' veya 'boş form' içeren genel metinleri ELE (Seçme) - ama içinde isim geçiyorsa MUTLAKA seç.
4. Eksiksiz liste için TÜM isim içeren alıntıları seç - bir alıntıda sadece 1 isim bile olsa, onu mutlaka seç.
5. Farklı alıntılarda farklı isimler olabilir - HEPSİNİ seç.
6. Dosya adlarında isim geçiyorsa o alıntıyı da seç (örn: "Aday Görüşme Özet _Elif Karadeniz.pdf" içeren alıntı).
7. Eksik liste vermek KESİNLİKLE YANLIŞ - TÜM isimleri bulana kadar TÜM alıntıları seç.
"""
            else:
                selection_instruction = """
Bu soru bir LİSTE sorusudur (Örn: 'Hangi firmalar', 'Kimler', 'Listele'). 
GÖREVİN:
1. İçinde SOMUT İSİM, FİRMA ADI, TEDARİKÇİ ADI veya VERİ geçen alıntıları seç.
2. Sadece 'prosedür tanımı', 'talimat' veya 'boş form' içeren genel metinleri ELE (Seçme).
3. Eksiksiz liste için en az 120-150 alakalı alıntı seçmeye çalış (genel kalite için artırıldı).
4. TÜM alakalı alıntıları seç - eksik bilgi vermemek için geniş bir seçim yap. "Şüpheli" olanları bile seç.
"""
    elif has_company_name and has_document_type:
        # Firma ismi ve belge türü içeren detay soruları için (örn: "SILA firmasına verilen teklif detayları")
        selection_instruction = """
Bu soru bir FİRMA/BELGE DETAY sorusudur (Örn: "X firmasına verilen teklif", "Y firması ile sözleşme"). 
GÖREVİN:
1. Soruda geçen FİRMA İSMİNİ içeren TÜM alıntıları seç (örn: soruda "SILA" geçiyorsa, "SILA" içeren tüm alıntıları seç).
2. Soruda geçen BELGE TÜRÜNÜ içeren alıntıları seç (teklif, sözleşme, fatura, PO, vb.).
3. Firma ismi ve belge türü birlikte geçen alıntıları ÖNCELİKLE seç.
4. Dosya adlarında firma ismi veya belge türü geçiyorsa o alıntıyı da seç.
5. E-posta adreslerinde firma domain'i varsa o alıntıyı da seç.
6. Sadece 'KVKK', 'prosedür tanımı', 'talimat' veya 'boş form' içeren genel metinleri ELE (Seçme) - ama içinde firma ismi veya belge türü geçiyorsa seç.
7. Eksiksiz bilgi için en az 50-80 alakalı alıntı seçmeye çalış - firma ismi ve belge türü ile ilgili TÜM alıntıları seç.
8. "Şüpheli" olanları bile seç - daha sonra filtrelenebilir.
"""
    elif has_company_name:
        # Sadece firma ismi içeren sorular için
        selection_instruction = """
Bu soru bir FİRMA DETAY sorusudur (Örn: "X firması", "Y şirketi"). 
GÖREVİN:
1. Soruda geçen FİRMA İSMİNİ içeren TÜM alıntıları seç (örn: soruda "SILA" geçiyorsa, "SILA" içeren tüm alıntıları seç).
2. Dosya adlarında firma ismi geçiyorsa o alıntıyı da seç.
3. E-posta adreslerinde firma domain'i varsa o alıntıyı da seç.
4. Sadece 'KVKK', 'prosedür tanımı', 'talimat' veya 'boş form' içeren genel metinleri ELE (Seçme) - ama içinde firma ismi geçiyorsa seç.
5. Eksiksiz bilgi için en az 40-60 alakalı alıntı seçmeye çalış - firma ismi ile ilgili TÜM alıntıları seç.
"""
    else:
        selection_instruction = "Sadece bu soruya en alakalı olanların NUMARALARINI listele."
    
    user_prompt = f"""Kullanıcının sorusu: "{question}"

Aşağıda numaralandırılmış alıntılar var. {selection_instruction}
Örnek format: "1, 3, 7, 12, 15, 20, 25, ..."

Alıntılar:
{numbered_chunks}

Sorunun cevabı için alakalı OLAN TÜM alıntıların numaralarını (virgülle ayırarak) yaz:"""
    
    try:
        # Reranking için ucuz model kullan
        llm = get_cheap_llm()
        response = llm.invoke([
            SystemMessage(content=RERANK_SYSTEM_PROMPT),
            HumanMessage(content=user_prompt)
        ])
        
        # Token tracking
        if token_tracker:
            input_tokens, output_tokens = extract_token_usage_from_response(response, "Reranking", user_prompt)
            token_tracker.add_usage(input_tokens, output_tokens, "Reranking (Chunk Yeniden Sıralama)", 
                                   estimated=(input_tokens == 0 and output_tokens == 0))
        
        # Yanıttan numaraları çıkar
        response_text = response.content if hasattr(response, 'content') else str(response)
        numbers = re.findall(r'\d+', response_text)
        selected_indices = [int(n) - 1 for n in numbers if 1 <= int(n) <= len(chunks_to_process)]
        
        if not selected_indices:
            # Firma ismi içeren sorular için daha fazla chunk döndür
            question_lower = normalize_text_for_matching(question)
            has_company_name = any(word in question_lower for word in ['firma', 'sirket', 'tedarikci', 'musteri', 'supplier', 'vendor', 'company', 'client', 'customer'])
            has_document_type = any(word in question_lower for word in ['teklif', 'sozlesme', 'fatura', 'po', 'purchase order', 'offer', 'invoice', 'contract'])
            
            if is_list_query:
                fallback_count = 100
            elif has_company_name and has_document_type:
                fallback_count = 50  # Firma ismi ve belge türü içeren sorular için
            elif has_company_name:
                fallback_count = 30  # Sadece firma ismi içeren sorular için
            else:
                fallback_count = 20
            print(f"⚠️ LLM hiçbir alıntı seçemedi, ilk {fallback_count} chunk döndürülüyor (daha fazla bilgi için).")
            return chunks_to_process[:fallback_count]
        
        reranked = [chunks_to_process[i] for i in selected_indices if 0 <= i < len(chunks_to_process)]
        
        # Minimum chunk sayısı garantisi
        question_lower = normalize_text_for_matching(question)
        has_company_name = any(word in question_lower for word in ['firma', 'sirket', 'tedarikci', 'musteri', 'supplier', 'vendor', 'company', 'client', 'customer'])
        has_document_type = any(word in question_lower for word in ['teklif', 'sozlesme', 'fatura', 'po', 'purchase order', 'offer', 'invoice', 'contract'])
        
        if is_list_query:
            # Liste soruları için minimum chunk garantisi
            is_supplier_query = has_company_name
            is_name_list_query = any(word in question_lower for word in ['isimleri', 'isimler', 'kimler', 'hangi.*aday', 'hangi.*kisi', 'nedir.*isim'])
            # İsim listesi soruları için daha fazla chunk gerekli (eksiksiz liste için)
            min_chunks = 250 if is_supplier_query else (150 if is_name_list_query else 120)  # İsim listesi soruları için minimum 150 chunk
            
            if len(reranked) < min_chunks:
                # Eğer toplam chunk sayısı minimum chunk sayısından azsa, tüm chunk'ları gönder
                if len(chunks_to_process) <= min_chunks:
                    print(f"⚠️ Liste sorusu için toplam {len(chunks_to_process)} chunk var (minimum {min_chunks} gerekli). Tüm chunk'lar gönderiliyor...")
                    reranked = chunks_to_process
                else:
                    print(f"⚠️ Liste sorusu için sadece {len(reranked)} chunk seçildi. En iyi {min_chunks} chunk'a tamamlanıyor (genel kalite için)...")
                    selected_set = set(selected_indices)
                    remaining_chunks = [(i, chunks_to_process[i]) for i in range(len(chunks_to_process)) if i not in selected_set]
                    remaining_chunks.sort(key=lambda x: x[1].metadata.get('similarity_score', 0.0), reverse=True)
                    # Toplam min_chunks chunk olana kadar ekle
                    for i, chunk in remaining_chunks[:min_chunks - len(reranked)]:
                        reranked.append(chunk)
        elif has_company_name and has_document_type:
            # Firma ismi ve belge türü içeren detay soruları için minimum chunk garantisi
            min_chunks = 50  # Firma ismi ve belge türü içeren sorular için minimum 50 chunk
            if len(reranked) < min_chunks:
                print(f"⚠️ Firma/belge detay sorusu için sadece {len(reranked)} chunk seçildi. En iyi {min_chunks} chunk'a tamamlanıyor...")
                selected_set = set(selected_indices)
                remaining_chunks = [(i, chunks_to_process[i]) for i in range(len(chunks_to_process)) if i not in selected_set]
                remaining_chunks.sort(key=lambda x: x[1].metadata.get('similarity_score', 0.0), reverse=True)
                # Toplam min_chunks chunk olana kadar ekle
                for i, chunk in remaining_chunks[:min_chunks - len(reranked)]:
                    reranked.append(chunk)
        elif has_company_name:
            # Sadece firma ismi içeren sorular için minimum chunk garantisi
            min_chunks = 30  # Firma ismi içeren sorular için minimum 30 chunk
            if len(reranked) < min_chunks:
                print(f"⚠️ Firma detay sorusu için sadece {len(reranked)} chunk seçildi. En iyi {min_chunks} chunk'a tamamlanıyor...")
                selected_set = set(selected_indices)
                remaining_chunks = [(i, chunks_to_process[i]) for i in range(len(chunks_to_process)) if i not in selected_set]
                remaining_chunks.sort(key=lambda x: x[1].metadata.get('similarity_score', 0.0), reverse=True)
                # Toplam min_chunks chunk olana kadar ekle
                for i, chunk in remaining_chunks[:min_chunks - len(reranked)]:
                    reranked.append(chunk)
        else:
            # Normal sorular için minimum chunk garantisi
            # "kaç adet", "toplamda kaç" gibi sayısal sorular için daha fazla chunk gerekli
            is_count_query = any(word in question_lower for word in ['kac', 'toplam', 'adet', 'sayi', 'count', 'total', 'how many'])
            min_chunks = 50 if is_count_query else 20  # Sayısal sorular için minimum 50 chunk
            
            if len(reranked) < min_chunks:
                # Eğer toplam chunk sayısı minimum chunk sayısından azsa, tüm chunk'ları gönder
                if len(chunks_to_process) <= min_chunks:
                    print(f"⚠️ Toplam {len(chunks_to_process)} chunk var (minimum {min_chunks} gerekli). Tüm chunk'lar gönderiliyor...")
                    reranked = chunks_to_process
                else:
                    print(f"⚠️ Sadece {len(reranked)} chunk seçildi. En iyi {min_chunks} chunk'a tamamlanıyor (doğruluk için)...")
                    selected_set = set(selected_indices)
                    remaining_chunks = [(i, chunks_to_process[i]) for i in range(len(chunks_to_process)) if i not in selected_set]
                    remaining_chunks.sort(key=lambda x: x[1].metadata.get('similarity_score', 0.0), reverse=True)
                    # Toplam min_chunks chunk olana kadar ekle
                    for i, chunk in remaining_chunks[:min_chunks - len(reranked)]:
                        reranked.append(chunk)
        
        print(f"✅ LLM {len(selected_indices)} alıntı seçti. {len(reranked)} alıntı yeniden sıralandı.")
        return reranked
    except Exception as e:
        print(f"❌ Reranking hatası: {e}. İlk 20 chunk döndürülüyor.")
        return chunks_to_process[:20]


def create_hypothetical_document_for_query_wrapper(question: str, model_name: str, token_tracker: TokenTracker = None) -> str:
    """HyDE: Sorudan hipotetik bir belge oluştur (vektör araması için)."""
    user_prompt = f"""Orijinal Soru: "{question}"

Bu soruya cevap verebilecek örnek bir belge metni:"""
    
    try:
        # HyDE için ucuz model kullan
        llm = get_cheap_llm()
        response = llm.invoke([
            SystemMessage(content=HYDE_SYSTEM_PROMPT),
            HumanMessage(content=user_prompt)
        ])
        
        # Token tracking
        if token_tracker:
            input_tokens, output_tokens = extract_token_usage_from_response(response, "HyDE (Hipotetik Belge Oluşturma)", user_prompt)
            token_tracker.add_usage(input_tokens, output_tokens, "HyDE (Hipotetik Belge Oluşturma)", 
                                   estimated=(input_tokens == 0 and output_tokens == 0))
        
        hyde_text = response.content if hasattr(response, 'content') else str(response)
        return hyde_text
    except Exception as e:
        print(f"HyDE oluşturma hatası: {e}")
        return question  # Hata durumunda orijinal soruyu döndür


# Basit in-memory cache (opsiyonel - performans için)
_off_topic_cache: dict[str, bool] = {}
_help_query_cache: dict[str, bool] = {}
_greeting_cache: dict[str, bool] = {}


def is_off_topic_query(query: str, use_cache: bool = True) -> bool:
    """
    LLM kullanarak sorgunun genel sohbet/off-topic olup olmadığını kontrol eder.
    Bu tür sorular dosyalarda taranmamalı ve yanıt verilmemelidir.
    
    Args:
        query: Kullanıcının sorusu
        use_cache: Cache kullanılsın mı (aynı sorular için tekrar LLM çağrısı yapılmasın)
    
    Returns:
        True eğer sorgu off-topic ise (genel sohbet, hal hatır, spor, hava durumu vb.)
        False eğer sorgu Knowvex ile ilgiliyse
    """
    if not query or not query.strip():
        return False
    
    # Cache kontrolü (opsiyonel - performans için)
    if use_cache:
        cache_key = query.lower().strip()
        cached_result = _off_topic_cache.get(cache_key)
        if cached_result is not None:
            return cached_result
    
    try:
        # Ucuz model kullan (classification için yeterli)
        llm = get_cheap_llm()
        
        prompt = f"""Sen bir Knowvex asistanısın. Kullanıcıların sorularını analiz edip, bu soruların platform ile ilgili olup olmadığını belirlemelisin.

KNOWVEX İLE İLGİLİ SORULAR:
- Dosya, belge, rapor, proje, prosedür, politika arama
- Mail içerikleri, kritik mailler, mail istatistikleri
- Veritabanı sorguları, veri analizi
- İş süreçleri, kurumsal bilgiler
- Şirket içi dokümantasyon, talimatlar
- Tarih, sayı, istatistik sorguları (iş ile ilgili)
- Detay bilgi, fiyat, miktar, tutar sorguları (dosyalarda arama yapılması gereken)
- "Detay bilgisi verir misin?", "fiyatı nedir?", "tutar nedir?" gibi sorular (dosyalarda arama yapılması gereken)
- Belge içeriği hakkında sorular ("nedir", "ne kadar", "kaç", "hangi", "kim")

OFF-TOPIC (GENEL SOHBET) SORULAR - YANIT VERİLMEMELİ:
- Hava durumu, havalar nasıl
- Spor, maç sonuçları, futbol, basketbol
- Genel sohbet, ne haber, naber (selamlaşma dışında)
- Kişisel sorular (sen kimsin, kaç yaşındasın)
- Manipüle edici sorular
- Platform dışı genel bilgi soruları

NOT: Selamlaşma ve hal hatır soruları ("merhaba", "nasılsın", "iyi günler") ayrı bir kategori olarak işlenir ve nazik bir şekilde cevaplanır.

KULLANICI SORUSU: "{query}"

GÖREV: Bu soru Knowvex ile ilgili mi yoksa genel sohbet/off-topic mi?

YANIT FORMATI: Sadece JSON formatında yanıt ver:
{{
    "is_off_topic": true/false,
    "reason": "kısa açıklama"
}}

ÖRNEKLER:
- "Bugün kaç mail geldi?" → {{"is_off_topic": false, "reason": "Mail istatistiği - platform ile ilgili"}}
- "Nasılsın?" → {{"is_off_topic": true, "reason": "Genel sohbet - hal hatır"}}
- "X projesi hakkında bilgi ver" → {{"is_off_topic": false, "reason": "Proje bilgisi - platform ile ilgili"}}
- "Havalar nasıl?" → {{"is_off_topic": true, "reason": "Hava durumu - off-topic"}}
- "Kritik mailleri listele" → {{"is_off_topic": false, "reason": "Mail sorgusu - platform ile ilgili"}}
- "Şu maç ne oldu?" → {{"is_off_topic": true, "reason": "Spor - off-topic"}}
- "Detay bilgisi verir misin? fatura fiyatı nedir?" → {{"is_off_topic": false, "reason": "Belge içeriği sorusu - dosyalarda arama yapılması gereken"}}
- "Fiyatı nedir?" → {{"is_off_topic": false, "reason": "Belge içeriği sorusu - dosyalarda arama yapılması gereken"}}
- "Tutar nedir?" → {{"is_off_topic": false, "reason": "Belge içeriği sorusu - dosyalarda arama yapılması gereken"}}
- "Ne kadar?" → {{"is_off_topic": false, "reason": "Belge içeriği sorusu - dosyalarda arama yapılması gereken"}}

YANIT:"""

        response = llm.invoke(prompt)
        response_text = response.content if hasattr(response, 'content') else str(response)
        
        # JSON'u parse et
        try:
            # JSON'u bul (```json ... ``` veya direkt JSON)
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                json_str = response_text[json_start:json_end].strip()
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                json_str = response_text[json_start:json_end].strip()
            else:
                json_str = response_text.strip()
            
            # İlk { ve son } arasını al
            if "{" in json_str and "}" in json_str:
                json_start = json_str.find("{")
                json_end = json_str.rfind("}") + 1
                json_str = json_str[json_start:json_end]
            
            result = json.loads(json_str)
            is_off_topic = result.get("is_off_topic", False)
            reason = result.get("reason", "")
            
            print(f"🔍 Off-topic kontrolü: '{query[:50]}...' → {'OFF-TOPIC' if is_off_topic else 'PLATFORM İLE İLGİLİ'} ({reason})")
            
            # Cache'e kaydet
            if use_cache:
                cache_key = query.lower().strip()
                _off_topic_cache[cache_key] = is_off_topic
            
            return is_off_topic
            
        except json.JSONDecodeError as e:
            print(f"⚠️ LLM yanıtı JSON parse edilemedi: {e}")
            print(f"   Ham yanıt: {response_text[:200]}")
            # Fallback: Yanıtta "true" veya "false" kelimesi var mı kontrol et
            response_lower = response_text.lower()
            if "true" in response_lower and "is_off_topic" in response_lower:
                return True
            elif "false" in response_lower and "is_off_topic" in response_lower:
                return False
            # Belirsizse, güvenli tarafta kal (off-topic değil kabul et)
            return False
            
    except Exception as e:
        print(f"⚠️ Off-topic kontrolü sırasında hata: {e}")
        # Hata durumunda güvenli tarafta kal (off-topic değil kabul et)
        return False


def clear_off_topic_cache():
    """Off-topic cache'ini temizle (test veya güncelleme için)"""
    global _off_topic_cache
    _off_topic_cache.clear()


def is_help_or_support_query(query: str, use_cache: bool = True) -> bool:
    """
    LLM kullanarak sorgunun teknik destek/yardım sorusu olup olmadığını kontrol eder.
    Bu tür sorular dosya taraması gerektirmez, direkt cevaplanabilir.
    
    Args:
        query: Kullanıcının sorusu
        use_cache: Cache kullanılsın mı (aynı sorular için tekrar LLM çağrısı yapılmasın)
    
    Returns:
        True eğer sorgu teknik destek/yardım sorusu ise (dosya taraması gerektirmez)
        False eğer sorgu normal içerik arama sorusu ise (dosya taraması gerekir)
    """
    if not query or not query.strip():
        return False
    
    # Cache kontrolü (opsiyonel - performans için)
    if use_cache:
        cache_key = query.lower().strip()
        cached_result = _help_query_cache.get(cache_key)
        if cached_result is not None:
            return cached_result
    
    try:
        # Ucuz model kullan (classification için yeterli)
        llm = get_cheap_llm()
        
        prompt = f"""Sen bir Knowvex asistanısın. Kullanıcıların sorularını analiz edip, bu soruların teknik destek/yardım sorusu olup olmadığını belirlemelisin.

TEKNİK DESTEK/YARDIM SORULARI (DOSYA TARAMASI GEREKTİRMEZ - DİREKT CEVAPLANABİLİR):
- Platform kullanımı ile ilgili sorular ("nasıl kullanırım", "nasıl yapılır", "kullanım kılavuzu")
- Sistem hakkında sorular ("dosyaları kaybettim", "dosyalar nerede", "nasıl arama yaparım")
- Yardım istekleri ("yardım", "destek", "ne yapmalıyım", "ne yapacağım")
- Platform özellikleri hakkında sorular ("hangi özellikler var", "ne yapabilirim")
- Teknik sorunlar ("çalışmıyor", "hata alıyorum", "bulamıyorum")
- Kullanım talimatları ("nasıl", "hangi adımlar", "yöntem")
- Genel arama istekleri ("dosya aramak istiyorum", "arama yapmak istiyorum", "nasıl arama yapabilirim", "arama nasıl yapılır")
- Platform kullanımı hakkında genel sorular ("ne yapabilirim", "hangi özellikler var", "nasıl kullanırım")

NORMAL İÇERİK ARAMA SORULARI (DOSYA TARAMASI GEREKTİRİR):
- Belirli bir dosya, belge, rapor, proje arama ("X projesi", "Y raporu", "Z belgesi")
- Mail içerikleri arama ("bugün kaç mail", "kritik mailler")
- Veri sorgulama ("hangi firmalar", "kaç kişi", "toplam")
- İş süreçleri, kurumsal bilgiler ("tedarikçiler", "müşteriler", "sözleşmeler")
- Belirli konu/konu arama ("X konusunda", "Y hakkında")

KULLANICI SORUSU: "{query}"

GÖREV: Bu soru teknik destek/yardım sorusu mu yoksa normal içerik arama sorusu mu?

YANIT FORMATI: Sadece JSON formatında yanıt ver:
{{
    "is_help_query": true/false,
    "reason": "kısa açıklama"
}}

ÖRNEKLER:
- "Dosyaları kaybettim ne yapacağım?" → {{"is_help_query": true, "reason": "Teknik destek - dosya taraması gerektirmez"}}
- "Nasıl arama yaparım?" → {{"is_help_query": true, "reason": "Platform kullanımı - dosya taraması gerektirmez"}}
- "Dosya aramak istiyorum" → {{"is_help_query": true, "reason": "Genel arama isteği - dosya taraması gerektirmez, kullanım talimatı verilmeli"}}
- "Arama yapmak istiyorum" → {{"is_help_query": true, "reason": "Genel arama isteği - dosya taraması gerektirmez, kullanım talimatı verilmeli"}}
- "X projesi hakkında bilgi ver" → {{"is_help_query": false, "reason": "İçerik arama - dosya taraması gerekir"}}
- "Bugün kaç mail geldi?" → {{"is_help_query": false, "reason": "Mail sorgusu - dosya taraması gerekir"}}
- "Yardım istiyorum" → {{"is_help_query": true, "reason": "Yardım isteği - dosya taraması gerektirmez"}}
- "Kritik mailleri listele" → {{"is_help_query": false, "reason": "Mail sorgusu - dosya taraması gerekir"}}
- "Nasıl kullanırım bu platformu?" → {{"is_help_query": true, "reason": "Kullanım sorusu - dosya taraması gerektirmez"}}

YANIT:"""

        response = llm.invoke(prompt)
        response_text = response.content if hasattr(response, 'content') else str(response)
        
        # JSON'u parse et
        try:
            # JSON'u bul (```json ... ``` veya direkt JSON)
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                json_str = response_text[json_start:json_end].strip()
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                json_str = response_text[json_start:json_end].strip()
            else:
                json_str = response_text.strip()
            
            # İlk { ve son } arasını al
            if "{" in json_str and "}" in json_str:
                json_start = json_str.find("{")
                json_end = json_str.rfind("}") + 1
                json_str = json_str[json_start:json_end]
            
            result = json.loads(json_str)
            is_help_query = result.get("is_help_query", False)
            reason = result.get("reason", "")
            
            print(f"🔍 Yardım/destek kontrolü: '{query[:50]}...' → {'YARDIM/DESTEK' if is_help_query else 'NORMAL ARAMA'} ({reason})")
            
            # Cache'e kaydet
            if use_cache:
                cache_key = query.lower().strip()
                _help_query_cache[cache_key] = is_help_query
            
            return is_help_query
            
        except json.JSONDecodeError as e:
            print(f"⚠️ LLM yanıtı JSON parse edilemedi: {e}")
            print(f"   Ham yanıt: {response_text[:200]}")
            # Fallback: Yanıtta "true" veya "false" kelimesi var mı kontrol et
            response_lower = response_text.lower()
            if "true" in response_lower and "is_help_query" in response_lower:
                return True
            elif "false" in response_lower and "is_help_query" in response_lower:
                return False
            # Belirsizse, güvenli tarafta kal (yardım sorusu değil kabul et)
            return False
            
    except Exception as e:
        print(f"⚠️ Yardım/destek kontrolü sırasında hata: {e}")
        # Hata durumunda güvenli tarafta kal (yardım sorusu değil kabul et)
        return False


def get_help_response(query: str) -> str:
    """
    Teknik destek/yardım soruları için uygun cevabı oluşturur.
    LLM kullanarak kullanıcıya yardımcı bir cevap üretir.
    """
    try:
        llm = get_cheap_llm()
        
        prompt = f"""Sen bir Knowvex asistanısın. Kullanıcıya Knowvex hakkında yardımcı bilgiler veriyorsun.

KULLANICI SORUSU: "{query}"

GÖREV: Kullanıcının sorusuna yardımcı, samimi ve bilgilendirici bir cevap ver. Knowvex özelliklerini açıkla ve kullanıcıya rehberlik et.

ÖNEMLİ: Eğer kullanıcı "dosya aramak istiyorum", "arama yapmak istiyorum" gibi genel bir arama isteği belirtiyorsa, ona NASIL arama yapacağını açıkla. Örnekler ver ve spesifik sorular sormasını öner.

KNOWVEX ÖZELLİKLERİ:
- Dosya ve belge arama: "X konusunu ara", "Y projesi hakkında bilgi ver" gibi spesifik sorular sorabilirsiniz
- Mail yönetimi: "Bugün kaç mail geldi?", "Kritik mailleri listele" gibi mail sorguları yapabilirsiniz
- Veri analizi: "Hangi firmalar", "Kaç kişi", "Toplam" gibi istatistiksel sorular sorabilirsiniz
- Belge özetleme: "X raporunu özetle", "Y belgesinin özeti" gibi isteklerde bulunabilirsiniz

YANIT: Kullanıcıya yardımcı olacak şekilde, samimi ve anlaşılır bir dille cevap ver. Knowvex özelliklerini örneklerle açıkla. Eğer kullanıcı genel bir arama isteği belirtiyorsa, ona spesifik sorular sormasını öner."""

        response = llm.invoke(prompt)
        answer = response.content if hasattr(response, 'content') else str(response)
        
        return answer.strip()
        
    except Exception as e:
        print(f"⚠️ Yardım cevabı oluşturulurken hata: {e}")
        # Fallback cevap - kullanıcının sorusuna göre özelleştirilmiş
        query_lower = query.lower() if query else ""
        if any(word in query_lower for word in ["dosya aramak", "arama yapmak", "arama istiyorum", "nasıl arama"]):
            return """Knowvex'te dosya aramak için spesifik sorular sorabilirsiniz:

**Örnek arama soruları:**
• "X projesi hakkında bilgi ver"
• "Y raporunu özetle"
• "Z konusunu ara"
• "Fatura ile ilgili dosyaları bul"
• "Sözleşme belgelerini listele"

**Nasıl arama yapılır:**
1. Aradığınız konu, proje veya belge hakkında spesifik bir soru sorun
2. Örneğin: "Fatura fiyatı nedir?" yerine "X firmasına verilen fatura fiyatı nedir?" gibi
3. Mail aramak için: "Bugün kaç mail geldi?", "Kritik mailleri listele" gibi sorular sorabilirsiniz

**Diğer özellikler:**
• Mail yönetimi: "Bugün kaç mail geldi?", "Kritik mailleri listele"
• Veri analizi: "Hangi firmalar", "Kaç kişi", "Toplam"
• Belge özetleme: "X raporunu özetle", "Y belgesinin özeti"

Hangi konuda arama yapmak istiyorsunuz? Size yardımcı olabilirim."""
        else:
            return """Knowvex'te şunları yapabilirsiniz:

• **Dosya ve belge arama**: "X konusunu ara", "Y projesi hakkında bilgi ver" gibi sorular sorabilirsiniz
• **Mail yönetimi**: "Bugün kaç mail geldi?", "Kritik mailleri listele" gibi mail sorguları yapabilirsiniz  
• **Veri analizi**: "Hangi firmalar", "Kaç kişi", "Toplam" gibi istatistiksel sorular sorabilirsiniz
• **Belge özetleme**: "X raporunu özetle", "Y belgesinin özeti" gibi isteklerde bulunabilirsiniz

Dosyalarınızı kaybetmişseniz, lütfen sistem yöneticinizle iletişime geçin. Knowvex içindeki dosyaları aramak için "X dosyasını ara" veya "Y belgesini bul" gibi sorular sorabilirsiniz."""


def clear_help_query_cache():
    """Yardım sorgusu cache'ini temizle (test veya güncelleme için)"""
    global _help_query_cache
    _help_query_cache.clear()


def is_greeting_query(query: str, use_cache: bool = True) -> bool:
    """
    LLM kullanarak sorgunun selamlaşma/hal hatır sorusu olup olmadığını kontrol eder.
    Bu tür sorulara nazik bir şekilde cevap verilmelidir.
    
    Args:
        query: Kullanıcının sorusu
        use_cache: Cache kullanılsın mı (aynı sorular için tekrar LLM çağrısı yapılmasın)
    
    Returns:
        True eğer sorgu selamlaşma/hal hatır sorusu ise
        False eğer sorgu başka bir tür soru ise
    """
    if not query or not query.strip():
        return False
    
    # Cache kontrolü (opsiyonel - performans için)
    if use_cache:
        cache_key = query.lower().strip()
        cached_result = _greeting_cache.get(cache_key)
        if cached_result is not None:
            return cached_result
    
    try:
        # Ucuz model kullan (classification için yeterli)
        llm = get_cheap_llm()
        
        prompt = f"""Sen bir Knowvex asistanısın. Kullanıcıların sorularını analiz edip, bu soruların selamlaşma/hal hatır sorusu olup olmadığını belirlemelisin.

SELAMLAŞMA/HAL HATIR SORULARI (NAZİK CEVAP VERİLMELİ):
- Selamlaşma: "merhaba", "selam", "günaydın", "iyi günler", "iyi akşamlar", "iyi geceler"
- Hal hatır: "nasılsın", "nasılsınız", "nasılsın?", "nasılsınız?"
- Kısa nezaket ifadeleri: "naber", "ne haber", "ne var ne yok"
- Sadece selamlaşma içeren kısa mesajlar

DİĞER SORULAR (SELAMLAŞMA DEĞİL):
- İçerik arama soruları: "X projesi", "Y raporu", "bugün kaç mail"
- Yardım soruları: "nasıl kullanırım", "yardım", "destek"
- Off-topic sorular: "havalar nasıl", "maç ne oldu", "spor"
- Teknik sorular: "dosyaları kaybettim", "hata alıyorum"

KULLANICI SORUSU: "{query}"

GÖREV: Bu soru selamlaşma/hal hatır sorusu mu?

YANIT FORMATI: Sadece JSON formatında yanıt ver:
{{
    "is_greeting": true/false,
    "reason": "kısa açıklama"
}}

ÖRNEKLER:
- "Merhaba" → {{"is_greeting": true, "reason": "Selamlaşma"}}
- "Nasılsın?" → {{"is_greeting": true, "reason": "Hal hatır sorusu"}}
- "Günaydın" → {{"is_greeting": true, "reason": "Selamlaşma"}}
- "İyi günler" → {{"is_greeting": true, "reason": "Selamlaşma"}}
- "X projesi hakkında bilgi ver" → {{"is_greeting": false, "reason": "İçerik arama sorusu"}}
- "Bugün kaç mail geldi?" → {{"is_greeting": false, "reason": "Mail sorgusu"}}
- "Nasıl kullanırım?" → {{"is_greeting": false, "reason": "Yardım sorusu"}}
- "Havalar nasıl?" → {{"is_greeting": false, "reason": "Off-topic soru"}}

YANIT:"""

        response = llm.invoke(prompt)
        response_text = response.content if hasattr(response, 'content') else str(response)
        
        # JSON'u parse et
        try:
            # JSON'u bul (```json ... ``` veya direkt JSON)
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                json_str = response_text[json_start:json_end].strip()
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                json_str = response_text[json_start:json_end].strip()
            else:
                json_str = response_text.strip()
            
            # İlk { ve son } arasını al
            if "{" in json_str and "}" in json_str:
                json_start = json_str.find("{")
                json_end = json_str.rfind("}") + 1
                json_str = json_str[json_start:json_end]
            
            result = json.loads(json_str)
            is_greeting = result.get("is_greeting", False)
            reason = result.get("reason", "")
            
            print(f"👋 Selamlaşma kontrolü: '{query[:50]}...' → {'SELAMLAŞMA' if is_greeting else 'DİĞER'} ({reason})")
            
            # Cache'e kaydet
            if use_cache:
                cache_key = query.lower().strip()
                _greeting_cache[cache_key] = is_greeting
            
            return is_greeting
            
        except json.JSONDecodeError as e:
            print(f"⚠️ LLM yanıtı JSON parse edilemedi: {e}")
            print(f"   Ham yanıt: {response_text[:200]}")
            # Fallback: Yanıtta "true" veya "false" kelimesi var mı kontrol et
            response_lower = response_text.lower()
            if "true" in response_lower and "is_greeting" in response_lower:
                return True
            elif "false" in response_lower and "is_greeting" in response_lower:
                return False
            # Belirsizse, güvenli tarafta kal (selamlaşma değil kabul et)
            return False
            
    except Exception as e:
        print(f"⚠️ Selamlaşma kontrolü sırasında hata: {e}")
        # Hata durumunda güvenli tarafta kal (selamlaşma değil kabul et)
        return False


def get_greeting_response(query: str) -> str:
    """
    Selamlaşma/hal hatır soruları için nazik bir cevap oluşturur.
    """
    query_lower = query.lower().strip()
    
    # Selamlaşma türüne göre uygun cevap
    if any(word in query_lower for word in ["günaydın", "good morning", "morning"]):
        greeting = "Günaydın! 😊"
    elif any(word in query_lower for word in ["iyi akşamlar", "good evening", "evening"]):
        greeting = "İyi akşamlar! 😊"
    elif any(word in query_lower for word in ["iyi geceler", "good night", "night"]):
        greeting = "İyi geceler! 😊"
    elif any(word in query_lower for word in ["iyi günler", "good day", "have a nice day"]):
        greeting = "İyi günler! 😊"
    elif any(word in query_lower for word in ["merhaba", "selam", "hello", "hi", "hey"]):
        greeting = "Merhaba! 😊"
    else:
        greeting = "Merhaba! 😊"
    
    # Hal hatır sorusu varsa
    if any(word in query_lower for word in ["nasılsın", "nasılsınız", "how are you", "how are"]):
        response = f"""{greeting} Ben iyiyim, teşekkür ederim! Size nasıl yardımcı olabilirim?

Knowvex'te şunları yapabilirsiniz:
• Dosya ve belge arama: "X konusunu ara", "Y projesi hakkında bilgi ver"
• Mail yönetimi: "Bugün kaç mail geldi?", "Kritik mailleri listele"
• Veri analizi: "Hangi firmalar", "Kaç kişi", "Toplam"
• Belge özetleme: "X raporunu özetle", "Y belgesinin özeti"

Size nasıl yardımcı olabilirim?"""
    else:
        response = f"""{greeting} Size nasıl yardımcı olabilirim?

Knowvex'te şunları yapabilirsiniz:
• Dosya ve belge arama: "X konusunu ara", "Y projesi hakkında bilgi ver"
• Mail yönetimi: "Bugün kaç mail geldi?", "Kritik mailleri listele"
• Veri analizi: "Hangi firmalar", "Kaç kişi", "Toplam"
• Belge özetleme: "X raporunu özetle", "Y belgesinin özeti"

Hangi konuda yardıma ihtiyacınız var?"""
    
    return response


def clear_greeting_cache():
    """Selamlaşma cache'ini temizle (test veya güncelleme için)"""
    global _greeting_cache
    _greeting_cache.clear()

