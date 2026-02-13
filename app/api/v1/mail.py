# backend/app/api/v1/mail.py - GÜNCELLENDİ (SSE ve RAM Optimizasyonlu)

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
# --- YENİ İMPORTLAR ---
from fastapi.responses import StreamingResponse
import asyncio
import json
# --- YENİ İMPORTLAR BİTTİ ---
from pydantic import BaseModel
from typing import Optional, List, Dict, Any, Tuple
from collections import Counter
from datetime import datetime, timedelta, timezone
from app.schemas.user import UserInDB
from app.dependencies import get_current_user, get_db_repository, get_storage_adapter
from app.repositories.base import BaseRepository
from app.storage_adapters.base import BaseStorageAdapter
from app.services.llm_providers import get_llm_for_model
from app.services import vector_service
from app.services.chat_helpers import is_off_topic_query, is_help_or_support_query, get_help_response, is_greeting_query, get_greeting_response
from app.core.config import GEMINI_API_KEY
from app.core import parsers
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
import uuid
import imaplib
import time
import io
import mimetypes

router = APIRouter()

# UTC+3 (Türkiye saati) için timezone helper fonksiyonları
TURKEY_TIMEZONE = timezone(timedelta(hours=3))

def get_today_start_utc3() -> datetime:
    """UTC+3 (Türkiye saati) için bugünün başlangıcını döndürür (00:00:00 UTC+3)."""
    now_utc3 = datetime.now(TURKEY_TIMEZONE)
    today_start_utc3 = datetime(now_utc3.year, now_utc3.month, now_utc3.day, tzinfo=TURKEY_TIMEZONE)
    # UTC'ye çevir (Firestore UTC kullanır)
    return today_start_utc3.astimezone(timezone.utc).replace(tzinfo=None)

def get_now_utc3() -> datetime:
    """UTC+3 (Türkiye saati) için şu anki zamanı UTC olarak döndürür."""
    now_utc3 = datetime.now(TURKEY_TIMEZONE)
    return now_utc3.astimezone(timezone.utc).replace(tzinfo=None)

# --- Şemalar (Değişiklik yok) ---
class MailSummary(BaseModel):
    id: str
    tenant_id: str
    sender: str
    subject: str
    body: Optional[str] = None  # Process-in-RAM: body=None, fetch on-demand
    has_full_content: bool = False  # Indicates if body needs to be fetched from IMAP
    summary: str
    is_critical: bool = False
    is_answered: bool = False
    attachments: List[str] = []
    attachment_summaries: Dict[str, str] = {}  # {filename: summary}
    potential_tasks: List[str] = []
    critical_dates: Dict[str, Any] = {}
    created_at: datetime
    received_at: datetime

class MailCreate(BaseModel):
    sender: str
    subject: str
    body: str
    received_at: Optional[datetime] = None

class MailQueryRequest(BaseModel):
    query: str
    date_range: Optional[str] = None  # "daily", "weekly", "custom"
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None

class MailStats(BaseModel):
    total_mails: int
    critical_mails: int
    unanswered_mails: int
    unread_mails: int  # Okunmamış mail sayısı

# --- Servis Fonksiyonları (Değişiklik yok) ---
# ... (process_thread_with_llm, process_mail_with_llm, 
#      process_attachment_with_llm, save_mail_attachments fonksiyonları
#      hiçbir değişiklik olmadan buraya gelecek) ...

def get_mail_collection(db: firestore.Client):
    return db.collection("mails")

def process_thread_with_llm(thread_mails: List[Dict[str, Any]], model_name: str = "gemini") -> Dict[str, Any]:
    """Bir mailleşme zincirini (thread) özetler. Tüm mailleri kronolojik sıraya göre birleştirip özetler."""
    if not thread_mails or len(thread_mails) == 0:
        return {"summary": "", "is_critical": False, "potential_tasks": [], "critical_dates": {}}
    
    # Tek mail ise normal işleme yap
    if len(thread_mails) == 1:
        mail = thread_mails[0]
        # Process-in-RAM: body might be None, use summary instead for LLM
        mail_body = mail.get("body") or mail.get("summary", "")
        return process_mail_with_llm(
            mail_body,
            mail.get("subject", ""),
            mail.get("sender", ""),
            mail.get("attachment_summaries"),
            model_name
        )
    
    # Tüm mailleri kronolojik sıraya göre birleştir
    sorted_mails = sorted(thread_mails, key=lambda x: x.get("received_at", datetime.now()))
    
    # Thread içeriğini oluştur
    thread_content = ""
    all_attachment_summaries = {}
    for idx, mail in enumerate(sorted_mails):
        received_at = mail.get("received_at", datetime.now())
        if isinstance(received_at, str):
            try:
                received_at = datetime.fromisoformat(received_at.replace('Z', '+00:00'))
            except:
                received_at = datetime.now()
        date_str = received_at.strftime("%Y-%m-%d %H:%M")
        
        # Process-in-RAM: body might be None, use summary instead
        mail_body = mail.get('body') or mail.get('summary', 'İçerik mevcut değil (on-demand fetch gerekli)')
        
        thread_content += f"\n\n--- Mail {idx + 1} ({date_str}) ---\n"
        thread_content += f"Gönderen: {mail.get('sender', 'Bilinmiyor')}\n"
        thread_content += f"Konu: {mail.get('subject', 'Konu yok')}\n"
        thread_content += f"İçerik:\n{mail_body}\n"
        
        # Attachment özetlerini birleştir
        mail_attachments = mail.get("attachment_summaries", {})
        if mail_attachments:
            for filename, summary in mail_attachments.items():
                all_attachment_summaries[f"{date_str}_{filename}"] = summary
    
    # LLM ile thread'i özetle
    llm = get_llm_for_model(model_name)
    
    attachment_text = ""
    if all_attachment_summaries:
        attachment_text = "\n\nEk Özetleri:\n"
        for filename, summary in all_attachment_summaries.items():
            attachment_text += f"- {filename}: {summary}\n"
    
    # İlk mailin subject'ini kullan (thread subject'i)
    thread_subject = sorted_mails[0].get("subject", "")
    # Re:, Fwd: gibi önekleri temizle
    while thread_subject.lower().startswith(('re:', 'fwd:', 'fw:')):
        if thread_subject.lower().startswith('re:'):
            thread_subject = thread_subject[3:].strip()
        elif thread_subject.lower().startswith('fwd:'):
            thread_subject = thread_subject[4:].strip()
        elif thread_subject.lower().startswith('fw:'):
            thread_subject = thread_subject[3:].strip()
    
    prompt = f"""Aşağıdaki mailleşme zincirini (thread) analiz et ve JSON formatında yanıt ver.
Bu bir mailleşme zinciridir, yani birden fazla mail birbiriyle ilgili ve bir konuşma oluşturuyor.
Tüm mail geçmişini dikkate alarak, konuşmanın tamamını özetle ve görevleri çıkar.

Cevaplarınızı mümkün olduğunca kısa, öz ve net tutun. Gereksiz açıklamalardan kaçının.

{{
  "summary": "Tüm mailleşme zincirinin kısa özeti (max 150 kelime). Konuşmanın başlangıcından sonuna kadar ne konuşuldu, hangi kararlar alındı, hangi sorunlar çözüldü?",
  "is_critical": true/false,
  "potential_tasks": ["görev1", "görev2"],
  "critical_dates": {{"contract_renewal": "tarih", "delivery": "tarih", "meeting": "tarih", "deadline": "tarih"}}
}}

Mailleşme Zinciri (Kronolojik Sıra):
Konu: {thread_subject}
{thread_content[:4000]}{attachment_text}"""
    
    try:
        response = llm.invoke(prompt)
        content = response.content if hasattr(response, 'content') else str(response)
        # JSON parse
        json_start = content.find('{')
        json_end = content.rfind('}') + 1
        if json_start >= 0 and json_end > json_start:
            result = json.loads(content[json_start:json_end])
        else:
            # Fallback: İlk mailin özetini kullan
            result = process_mail_with_llm(
                sorted_mails[0].get("body", ""),
                thread_subject,
                sorted_mails[0].get("sender", ""),
                sorted_mails[0].get("attachment_summaries"),
                model_name
            )
    except Exception as e:
        print(f"⚠️ Thread özetleme hatası: {e}")
        # Fallback: İlk mailin özetini kullan
        result = process_mail_with_llm(
            sorted_mails[0].get("body", ""),
            thread_subject,
            sorted_mails[0].get("sender", ""),
            sorted_mails[0].get("attachment_summaries"),
            model_name
        )
    
    return result

def process_mail_with_llm(mail_body: str, subject: str, sender: str, attachment_summaries: Dict[str, str] = None, model_name: str = "gemini") -> Dict[str, Any]:
    """Mail içeriğini LLM ile işleyerek özet, görevler ve kritik tarihleri çıkarır."""
    llm = get_llm_for_model(model_name)
    
    attachment_text = ""
    if attachment_summaries:
        attachment_text = "\n\nEk Özetleri:\n"
        for filename, summary in attachment_summaries.items():
            attachment_text += f"- {filename}: {summary}\n"
    
    prompt = f"""Aşağıdaki maili analiz et ve JSON formatında yanıt ver:

Cevaplarınızı mümkün olduğunca kısa, öz ve net tutun. Gereksiz açıklamalardan kaçının.

{{
  "summary": "Mailin kısa özeti (max 100 kelime)",
  "is_critical": true/false,
  "potential_tasks": ["görev1", "görev2"],
  "critical_dates": {{"contract_renewal": "tarih", "delivery": "tarih", "meeting": "tarih", "deadline": "tarih"}}
}}

ÖNEMLİ: critical_dates için tarihleri YYYY-MM-DD formatında ver (örn: "2024-12-25"). 
Eğer tam tarih belirtilmemişse ve sadece "yarın", "gelecek hafta" gibi ifadeler varsa, bugünün tarihi {datetime.now().strftime('%Y-%m-%d')} olduğunu dikkate alarak hesapla ve YYYY-MM-DD formatında yaz.
Eğer hiç tarih bilgisi yoksa o alanı boş bırak.

Mail:
Gönderen: {sender}
Konu: {subject}
İçerik: {mail_body[:2000]}{attachment_text}"""
    
    try:
        response = llm.invoke(prompt)
        content = response.content if hasattr(response, 'content') else str(response)
        # JSON parse
        json_start = content.find('{')
        json_end = content.rfind('}') + 1
        if json_start >= 0 and json_end > json_start:
            result = json.loads(content[json_start:json_end])
        else:
            result = {"summary": content[:200], "is_critical": False, "potential_tasks": [], "critical_dates": {}}
    except:
        result = {"summary": f"{subject} - {mail_body[:200]}", "is_critical": False, "potential_tasks": [], "critical_dates": {}}
    
    return result

def process_attachment_with_llm(attachment_content: str, filename: str, model_name: str = "gemini") -> tuple[str, List[Dict[str, Any]]]:
    """Ek içeriğini LLM ile özetler. Fiyat, tarih ve önemli bilgileri içerir. Tablo verilerini de döndürür."""
    if not attachment_content or len(attachment_content.strip()) < 10:
        print(f"⚠️ Ek içeriği çok kısa: {filename} - {len(attachment_content) if attachment_content else 0} karakter")
        return "Ek içeriği okunamadı veya çok kısa.", []
    
    print(f"🤖 LLM ile ek özeti oluşturuluyor: {filename} ({len(attachment_content)} karakter)")
    print(f"📄 İçerik önizleme (ilk 500 karakter): {attachment_content[:500]}")
    
    llm = get_llm_for_model(model_name)
    
    # Daha fazla içerik oku (fiyat ve detaylar için) - PDF'ler için daha fazla karakter
    # Tüm içeriği analiz et (limit artırıldı)
    content_to_analyze = attachment_content[:15000] if len(attachment_content) > 10000 else attachment_content
    
    prompt = f"""Aşağıdaki ek dosyasının ({filename}) detaylı özetini çıkar ve JSON formatında yanıt ver.

Cevaplarınızı mümkün olduğunca kısa, öz ve net tutun. Gereksiz açıklamalardan kaçının.

JSON formatı:
{{
  "summary": "Doğal, akıcı bir metin özeti. Ürün isimleri, miktarlar, fiyatlar, tarihler ve diğer önemli bilgileri içerir.",
  "table_data": [
    {{
      "ürün": "Ürün adı",
      "miktar": "Miktar ve birim (örn: 100 kg)",
      "birim_fiyat": "Birim fiyat (örn: 50 TL)",
      "toplam": "Toplam tutar (örn: 5000 TL)"
    }}
  ]
}}

ÖNEMLİ:
- Eğer dosyada tablo verileri varsa (ürün listesi, fiyat listesi vb.), bunları table_data array'ine ekle
- Her satır için ürün, miktar, birim_fiyat ve toplam bilgilerini dahil et
- Eğer tablo verisi yoksa, table_data boş array [] olsun
- summary'de tüm önemli bilgileri (tarihler, koşullar, notlar vb.) doğal bir şekilde anlat
- Sayıları, fiyatları ve tarihleri açıkça belirt

Dosya içeriği:
{content_to_analyze}"""
    
    try:
        response = llm.invoke(prompt)
        result_text = response.content if hasattr(response, 'content') else str(response)
        
        # JSON'u parse et
        import json
        try:
            # JSON'u bul (```json ... ``` veya direkt JSON)
            if "```json" in result_text:
                json_start = result_text.find("```json") + 7
                json_end = result_text.find("```", json_start)
                json_str = result_text[json_start:json_end].strip()
            elif "```" in result_text:
                json_start = result_text.find("```") + 3
                json_end = result_text.find("```", json_start)
                json_str = result_text[json_start:json_end].strip()
            else:
                # Direkt JSON olabilir
                json_str = result_text.strip()
            
            # İlk { ve son } arasını al
            if "{" in json_str and "}" in json_str:
                json_start = json_str.find("{")
                json_end = json_str.rfind("}") + 1
                json_str = json_str[json_start:json_end]
            
            result = json.loads(json_str)
            summary = result.get("summary", result_text[:1000])
            table_data = result.get("table_data", [])
            
            print(f"✅ LLM özeti oluşturuldu: {filename} - {len(summary)} karakter")
            print(f"📊 Tablo verisi: {len(table_data)} satır")
            if table_data:
                print(f"📄 Tablo önizleme: {table_data[0] if table_data else 'Yok'}")
            
            return summary[:1000].strip(), table_data
        except json.JSONDecodeError as e:
            print(f"⚠️ JSON parse hatası, sadece metin özeti kullanılıyor: {e}")
            print(f"   Ham yanıt: {result_text[:500]}")
            return result_text[:1000].strip(), []
    except Exception as e:
        print(f"❌ LLM özeti oluşturma hatası: {filename} - {str(e)}")
        import traceback
        print(traceback.format_exc())
        return f"Ek özeti oluşturulamadı: {str(e)}", []

async def save_mail_attachments(attachments: List[UploadFile], tenant_id: str, mail_id: str, storage: BaseStorageAdapter) -> List[str]:
    """Mail eklerini kaydeder ve storage path'lerini döndürür."""
    saved_paths = []
    for att in attachments:
        unique_name = f"{uuid.uuid4()}_{att.filename}"
        path = storage.upload_file(att.file, tenant_id, f"mail_attachments/{mail_id}/{unique_name}")
        saved_paths.append(path)
    return saved_paths


# --- API Endpoint'leri ---

# --- BELLEK OPTİMİZASYONU SABİTLERİ ---
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10MB limit - daha büyük attachment'lar atlanacak
MAX_ATTACHMENT_SIZE_FOR_PROCESSING = 5 * 1024 * 1024  # 5MB - daha büyük attachment'lar sadece kaydedilecek, LLM işleme yapılmayacak
MAX_MAILS_PER_BATCH = 50  # Her 50 mail'de bir bellek temizliği yapılacak

# --- GÜNCELLEME BURADA: /fetch endpoint'i SSE kullanacak şekilde değiştirildi ---
@router.post("/fetch")
async def fetch_and_process_mails(
    limit: int = 1000,  # Tüm mailleri çekmek için yüksek limit
    since_date: Optional[str] = None,  # Tarih filtresi (YYYY-MM-DD formatında)
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository),
    storage: BaseStorageAdapter = Depends(get_storage_adapter)
):
    """IMAP'ten mailleri çeker ve işler (SSE ile anlık akış sağlar)."""
    
    # --- YENİ: Asenkron Generator Fonksiyonu ---
    # Tüm mail çekme mantığı bu fonksiyonun içinde çalışacak
    # ve her adımı 'yield' ile dışarıya (frontend'e) gönderecek.
    async def event_stream_generator():
        from app.services.mail_service import fetch_mails
        
        # Sadece admin mail çekebilir
        if current_user.role != "Admin":
            error_data = {"step": 0, "message": "Sadece admin kullanıcılar mail çekebilir.", "status": "error"}
            yield f"data: {json.dumps(error_data)}\n\n"
            return
        
        firestore_db = firestore.Client()
        settings_doc = firestore_db.collection("mail_settings").document(current_user.tenant_id).get()
        if not settings_doc.exists:
            error_data = {"step": 0, "message": "Mail ayarları bulunamadı.", "status": "error"}
            yield f"data: {json.dumps(error_data)}\n\n"
            return
        
        settings = settings_doc.to_dict()
        email_address = settings.get("email_address", "")
        password = settings.get("password", "")
        imap_server = settings.get("imap_server", "")
        imap_port = settings.get("imap_port", 993)
        fetch_unread_only_setting = settings.get("fetch_unread_only", True)
        
        print(f"🔍 Firestore'dan alınan fetch_unread_only değeri: {fetch_unread_only_setting} (tip: {type(fetch_unread_only_setting).__name__})")
        
        if isinstance(fetch_unread_only_setting, bool):
            fetch_unread_only = fetch_unread_only_setting
        elif isinstance(fetch_unread_only_setting, str):
            fetch_unread_only = fetch_unread_only_setting.lower() in ['true', '1', 'yes']
        elif fetch_unread_only_setting is None:
            fetch_unread_only = True
        else:
            fetch_unread_only = bool(fetch_unread_only_setting)
        
        print(f"📧 Mail çekme modu: {'Sadece okunmamış' if fetch_unread_only else 'Tüm mailler'} (fetch_unread_only={fetch_unread_only})")
        
        # Kullanıcıya bilgi ver
        step_data = {"step": 0.5, "message": f"📧 Mail çekme modu: {'Sadece okunmamış mailler' if fetch_unread_only else 'Tüm mailler (okunmuş + okunmamış)'}", "status": "info"}
        yield f"data: {json.dumps(step_data)}\n\n"
        await asyncio.sleep(0.01)
        
        if not email_address or not password:
            error_data = {"step": 0, "message": "Mail ayarları eksik.", "status": "error"}
            yield f"data: {json.dumps(error_data)}\n\n"
            return
        
        # 'steps' listesi kaldırıldı, artık her adımda 'yield' kullanılacak.
        try:
            step_data = {"step": 1, "message": "IMAP sunucusuna bağlanılıyor...", "status": "info"}
            yield f"data: {json.dumps(step_data)}\n\n"
            await asyncio.sleep(0.01) # Event loop'a nefes aldır
            
            since_datetime = None
            if since_date:
                try:
                    since_datetime = datetime.strptime(since_date, "%Y-%m-%d")
                    step_data = {"step": 1.5, "message": f"📅 Tarih filtresi: {since_date} tarihinden itibaren mailler çekilecek...", "status": "info"}
                    yield f"data: {json.dumps(step_data)}\n\n"
                    await asyncio.sleep(0.01)
                except ValueError:
                    step_data = {"step": 1.5, "message": f"⚠️ Geçersiz tarih formatı: {since_date}, tüm mailler çekilecek", "status": "warning"}
                    yield f"data: {json.dumps(step_data)}\n\n"
                    await asyncio.sleep(0.01)
            
            fetched_mails_generator = fetch_mails(email_address, password, imap_server, imap_port, limit, fetch_unread_only, since_datetime)
            
            step_data = {"step": 2, "message": "Mail listesi alındı, işleme başlanıyor...", "status": "info"}
            yield f"data: {json.dumps(step_data)}\n\n"
            await asyncio.sleep(0.01)
            
            processed_count = 0
            skipped_count = 0
            errors = []
            mail_col = get_mail_collection(firestore_db)
            total_start_time = time.time()
            mail_processing_times = []
            total_fetched = 0
            processed_in_batch = 0  # Batch sayacı
            
            print(f"📬 Mail işleme döngüsü başlıyor...")
            for mail_data in fetched_mails_generator:
                total_fetched += 1
                mail_start_time = time.time()
                try:
                    message_id = mail_data.get("message_id", "")
                    email_id = mail_data.get("email_id", "")
                    in_reply_to = mail_data.get("in_reply_to", "")
                    
                    thread_id = message_id
                    if in_reply_to:
                        parent_query = (
                            mail_col.where(filter=FieldFilter("tenant_id", "==", current_user.tenant_id))
                            .where(filter=FieldFilter("message_id", "==", in_reply_to))
                            .limit(1)
                        )
                        parent_docs = list(parent_query.stream())
                        if parent_docs:
                            parent_data = parent_docs[0].to_dict()
                            thread_id = parent_data.get("thread_id") or parent_data.get("message_id") or in_reply_to
                            print(f"🔗 Mail thread'e eklendi (parent: {in_reply_to}, thread_id: {thread_id})")
                        else:
                            thread_id = in_reply_to
                            print(f"🔗 Parent mail henüz kaydedilmemiş, thread_id: {thread_id}")
                    
                    if message_id:
                        existing_query = (
                            mail_col.where(filter=FieldFilter("tenant_id", "==", current_user.tenant_id))
                            .where(filter=FieldFilter("message_id", "==", message_id))
                            .limit(1)
                        )
                        existing_docs = list(existing_query.stream())
                        if existing_docs:
                            skipped_count += 1
                            mail_processing_times.append(time.time() - mail_start_time)
                            subject_short = mail_data.get('subject', 'Bilinmiyor')[:50]
                            step_data = {"step": 3 + processed_count + skipped_count, "message": f"⏭️ Atlandı (zaten işlenmiş): {subject_short}...", "status": "skip"}
                            yield f"data: {json.dumps(step_data)}\n\n"
                            await asyncio.sleep(0.01)
                            print(f"⏭️ Mail zaten işlenmiş (Message-ID: {message_id}), atlandı: {mail_data.get('subject', 'Bilinmiyor')}")
                            continue
                    
                    if not message_id or message_id.startswith("imap_uid_"):
                        if email_id:
                            existing_query = (
                                mail_col.where(filter=FieldFilter("tenant_id", "==", current_user.tenant_id))
                                .where(filter=FieldFilter("email_id", "==", email_id))
                                .limit(1)
                            )
                            existing_docs = list(existing_query.stream())
                            if existing_docs:
                                skipped_count += 1
                                mail_processing_times.append(time.time() - mail_start_time)
                                subject_short = mail_data.get('subject', 'Bilinmiyor')[:50]
                                step_data = {"step": 3 + processed_count + skipped_count, "message": f"⏭️ Atlandı (zaten işlenmiş): {subject_short}...", "status": "skip"}
                                yield f"data: {json.dumps(step_data)}\n\n"
                                await asyncio.sleep(0.01)
                                print(f"⏭️ Mail zaten işlenmiş (IMAP UID: {email_id}), atlandı: {mail_data.get('subject', 'Bilinmiyor')}")
                                continue
                    
                    doc_ref = mail_col.document()
                    mail_id = doc_ref.id
                    
                    subject_short = mail_data.get('subject', 'Bilinmiyor')[:50]
                    current_step = 3 + processed_count + skipped_count + 1
                    
                    step_data = {"step": current_step, "message": f"📧 İşleniyor (Mail {processed_count + skipped_count + 1}): {subject_short}...", "status": "processing"}
                    yield f"data: {json.dumps(step_data)}\n\n"
                    await asyncio.sleep(0.01) # Akış için bekle
                    
                    # --- Process-in-RAM: Ekleri RAM'de işle, dosyayı kaydetme ---
                    attachment_summaries = {}
                    attachment_tables = {}
                    saved_attachment_filenames = []  # Sadece dosya adları (path değil)
                    attachment_count = len(mail_data.get("attachments", []))
                    
                    if attachment_count > 0:
                        step_data = {"step": current_step + 0.1, "message": f"   📎 {attachment_count} ek dosyası bulundu, RAM'de işleniyor...", "status": "info"}
                        yield f"data: {json.dumps(step_data)}\n\n"
                        await asyncio.sleep(0.01)
                        
                        # --- HAFIZA OPTİMİZASYONU: KOPYA LİSTE ---
                        attachments_to_process = list(mail_data.get("attachments", []))

                        for att in attachments_to_process:
                            filename = att.get("filename", "")
                            payload = att.get("payload")  # payload'u al
                            content_type = att.get("content_type", "")
                            
                            # BOYUT KONTROLÜ - Çok büyük attachment'ları atla (>10MB)
                            if payload and len(payload) > MAX_ATTACHMENT_SIZE:
                                step_data = {"step": current_step + 0.2, "message": f"   ⚠️ Ek çok büyük, atlanıyor: {filename} ({len(payload) / 1024 / 1024:.1f}MB > 10MB)", "status": "warning"}
                                yield f"data: {json.dumps(step_data)}\n\n"
                                await asyncio.sleep(0.01)
                                # Payload'ı hemen temizle
                                if 'payload' in att:
                                    att['payload'] = None
                                    del att['payload']
                                del payload
                                import gc
                                gc.collect()
                                continue
                            
                            if payload and filename:
                                try:
                                    payload_size = len(payload) if payload else 0
                                    print(f"📎 Ek RAM'de işleniyor: {filename} (tip: {content_type}, boyut: {payload_size / 1024 / 1024:.2f}MB)")
                                    
                                    # Process-in-RAM: Dosyayı kaydetme, direkt RAM'de işle
                                    # Sadece küçük attachment'lar için işleme yap (5MB limit)
                                    if payload_size <= MAX_ATTACHMENT_SIZE_FOR_PROCESSING:
                                        # Truncate very large text before extraction to avoid OOM
                                        if payload_size > 5 * 1024 * 1024:  # 5MB
                                            payload = payload[:5 * 1024 * 1024]
                                            print(f"⚠️ Ek 5MB'den büyük, ilk 5MB işlenecek: {filename}")
                                        
                                        # Extract text from bytes immediately (in RAM)
                                        attachment_text = parsers.extract_text_from_file(
                                            file_bytes=payload,
                                            file_name=filename,
                                            mime_type=content_type
                                        )
                                        print(f"📄 Ek içeriği çıkarıldı: {len(attachment_text) if attachment_text else 0} karakter")
                                        
                                        # Immediately discard payload to free RAM
                                        del payload
                                        import gc
                                        gc.collect()
                                        
                                        if attachment_text and len(attachment_text.strip()) > 10 and not attachment_text.strip().startswith("["):
                                            step_data = {"step": current_step + 0.2, "message": f"   🤖 Ek özeti oluşturuluyor: {filename}...", "status": "info"}
                                            yield f"data: {json.dumps(step_data)}\n\n"
                                            await asyncio.sleep(0.01)
                                            
                                            # Process attachment with LLM to get summary
                                            att_summary, att_table = process_attachment_with_llm(attachment_text, filename)
                                            attachment_summaries[filename] = att_summary
                                            if att_table:
                                                attachment_tables[filename] = att_table
                                            
                                            step_data = {"step": current_step + 0.3, "message": f"   ✅ Ek özeti oluşturuldu: {filename}", "status": "success"}
                                            yield f"data: {json.dumps(step_data)}\n\n"
                                            await asyncio.sleep(0.01)
                                            print(f"✅ Ek özeti oluşturuldu: {filename} - {att_summary[:100]}")
                                            
                                            # Attachment text'i temizle
                                            del attachment_text
                                            import gc
                                            gc.collect()
                                        else:
                                            error_msg = f"Ek içeriği okunamadı veya çok kısa: {filename}"
                                            print(f"⚠️ {error_msg}")
                                            step_data = {"step": current_step + 0.2, "message": f"   ⚠️ Ek içeriği okunamadı: {filename}", "status": "warning"}
                                            yield f"data: {json.dumps(step_data)}\n\n"
                                            await asyncio.sleep(0.01)
                                    
                                    # Dosya adını kaydet (path değil, sadece filename)
                                    saved_attachment_filenames.append(filename)
                                    
                                    # --- HAFIZA TEMİZLEME (Kritik) - Daha agresif ---
                                    if 'payload' in att:
                                        att['payload'] = None
                                        del att['payload']
                                    if 'payload' in locals():
                                        del payload
                                    import gc
                                    gc.collect()
                                    # --- TEMİZLEME BİTTİ ---

                                except Exception as e:
                                    print(f"⚠️ Ek işleme hatası ({filename}): {e}")
                                    saved_attachment_filenames.append(filename)  # Hata durumunda da filename'i kaydet
                                    # Hata durumunda da temizle
                                    if 'payload' in att:
                                        att['payload'] = None
                                        del att['payload']
                                    if 'payload' in locals():
                                        del payload
                                    import gc
                                    gc.collect()
                        
                        # --- HAFIZA TEMİZLEME (Döngü sonrası) ---
                        if 'attachments' in mail_data:
                            mail_data['attachments'] = []
                        del attachments_to_process
                        import gc
                        gc.collect()

                    # --- LLM ile İşleme ---
                    step_data = {"step": current_step + 0.5, "message": f"   🤖 Mail içeriği analiz ediliyor (LLM)...", "status": "info"}
                    yield f"data: {json.dumps(step_data)}\n\n"
                    await asyncio.sleep(0.01)
                    
                    processed = process_mail_with_llm(
                        mail_data["body"], 
                        mail_data["subject"], 
                        mail_data["sender"],
                        attachment_summaries if attachment_summaries else None
                    )
                    
                    step_data = {"step": current_step + 0.6, "message": f"   ✅ Mail analizi tamamlandı (özet, görevler, kritik tarihler çıkarıldı)", "status": "success"}
                    yield f"data: {json.dumps(step_data)}\n\n"
                    await asyncio.sleep(0.01)
                    
                    # Process-in-RAM: body=None, has_full_content=False, attachments=filenames only
                    mail_record = {
                        "tenant_id": current_user.tenant_id,
                        "sender": mail_data["sender"],
                        "subject": mail_data["subject"],
                        "body": None,  # Privacy/Space optimization - body not saved
                        "has_full_content": False,  # Indicates body must be fetched on-demand
                        "message_id": message_id,
                        "email_id": email_id,
                        "in_reply_to": in_reply_to,
                        "thread_id": thread_id,
                        "summary": processed.get("summary", ""),
                        "is_critical": processed.get("is_critical", False),
                        "is_answered": False,
                        "is_read": False,
                        "attachments": saved_attachment_filenames,  # Only filenames, not paths
                        "attachment_summaries": attachment_summaries,  # {filename: summary}
                        "attachment_tables": attachment_tables,
                        "potential_tasks": processed.get("potential_tasks", []),
                        "critical_dates": processed.get("critical_dates", {}),
                        "created_at": datetime.now(),
                        "received_at": mail_data["received_at"]
                    }
                    
                    mail_record["id"] = mail_id
                    doc_ref.set(mail_record)
                    
                    # --- Vektör Veritabanı ---
                    try:
                        step_data = {"step": current_step + 0.7, "message": f"   🔍 Vektör veritabanına ekleniyor (soru-cevap için)...", "status": "info"}
                        yield f"data: {json.dumps(step_data)}\n\n"
                        await asyncio.sleep(0.01)
                        
                        embedding_model = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004", google_api_key=GEMINI_API_KEY)
                        
                        # Construct chunk_text with attachment summaries for vector search
                        attachment_summaries_text = ""
                        if attachment_summaries:
                            attachment_summaries_text = "\n\nEk Özetleri:\n"
                            for filename, summary in attachment_summaries.items():
                                attachment_summaries_text += f"- {filename}: {summary}\n"
                        
                        mail_text = f"Gönderen: {mail_data['sender']}\nKonu: {mail_data['subject']}\nMail Özeti: {processed.get('summary', '')}{attachment_summaries_text}"
                        embedding = embedding_model.embed_documents([mail_text])[0]
                        if embedding:
                            chunk_data = {
                                "tenant_id": current_user.tenant_id,
                                "file_id": f"mail_{mail_id}",
                                "file_name": f"Mail: {mail_data['subject']}",
                                "chunk_number": 0,
                                "chunk_text": mail_text,
                                "embedding": embedding,
                                "mail_id": mail_id
                            }
                            db.add_text_chunks_batch([chunk_data])
                            step_data = {"step": current_step + 0.8, "message": f"   ✅ Vektör veritabanına eklendi", "status": "success"}
                            yield f"data: {json.dumps(step_data)}\n\n"
                            await asyncio.sleep(0.01)
                    except Exception as e:
                        print(f"Vektör ekleme hatası: {e}")
                        errors.append(f"Vektör ekleme hatası (Mail: {mail_data['subject']}): {str(e)}")
                        step_data = {"step": current_step + 0.8, "message": f"   ⚠️ Vektör ekleme hatası: {str(e)[:50]}", "status": "error"}
                        yield f"data: {json.dumps(step_data)}\n\n"
                        await asyncio.sleep(0.01)
                    
                    processed_count += 1
                    processed_in_batch += 1
                    mail_processing_time = time.time() - mail_start_time
                    mail_processing_times.append(mail_processing_time)
                    
                    step_data = {"step": current_step + 1, "message": f"✅ Mail başarıyla işlendi ({mail_processing_time:.2f}s)", "status": "success"}
                    yield f"data: {json.dumps(step_data)}\n\n"
                    await asyncio.sleep(0.01)
                    print(f"✅ Mail işlendi ({processed_count}): {mail_data.get('subject', 'Bilinmiyor')[:50]}... ({mail_processing_time:.2f}s)")
                    
                    # Her 50 mail'de bir bellek temizliği yap
                    if processed_in_batch >= MAX_MAILS_PER_BATCH:
                        import gc
                        gc.collect()
                        processed_in_batch = 0
                        step_data = {"step": current_step + 1.1, "message": f"   🧹 Bellek temizliği yapıldı ({processed_count} mail işlendi)", "status": "info"}
                        yield f"data: {json.dumps(step_data)}\n\n"
                        await asyncio.sleep(0.01)
                
                except Exception as e:
                    mail_processing_time = time.time() - mail_start_time
                    mail_processing_times.append(mail_processing_time)
                    error_msg = f"Mail işleme hatası (Konu: {mail_data.get('subject', 'Bilinmiyor')}): {str(e)}"
                    subject_short = mail_data.get('subject', 'Bilinmiyor')[:50]
                    current_step = 3 + processed_count + skipped_count + 1
                    step_data = {"step": current_step, "message": f"❌ Hata: {subject_short}... - {str(e)[:100]}", "status": "error"}
                    yield f"data: {json.dumps(step_data)}\n\n"
                    await asyncio.sleep(0.01)
                    print(error_msg)
                    errors.append(error_msg)
                
                finally:
                    # --- HAFIZA TEMİZLEME (Ana döngü sonu) ---
                    if 'mail_data' in locals():
                        # Mail data içindeki büyük objeleri temizle
                        if 'body' in mail_data:
                            mail_data['body'] = None
                        if 'attachments' in mail_data:
                            mail_data['attachments'] = []
                        del mail_data
                    import gc
                    gc.collect()
            
            # --- Döngü Sonu ---
            if total_fetched == 0:
                step_data = {"step": 4, "message": "Hiç mail bulunamadı.", "status": "warning"}
                yield f"data: {json.dumps(step_data)}\n\n"
                await asyncio.sleep(0.01)
                return # Generator'ı bitir
            
            # --- İstatistikleri Hesapla ve Gönder ---
            total_time = time.time() - total_start_time
            avg_processing_time = sum(mail_processing_times) / len(mail_processing_times) if mail_processing_times else 0
            
            # Eğer mailler bulundu ama hepsi duplicate ise, özel mesaj göster
            if total_fetched > 0 and processed_count == 0 and skipped_count > 0:
                summary_steps = [
                    {"step": 999, "message": "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "status": "info"},
                    {"step": 1000, "message": f"📊 İşleme Tamamlandı", "status": "success"},
                    {"step": 1001, "message": f"   • Toplam mail: {total_fetched}", "status": "info"},
                    {"step": 1002, "message": f"   • İşlenen: {processed_count}", "status": "info"},
                    {"step": 1003, "message": f"   • Atlanan (zaten işlenmiş): {skipped_count}", "status": "skip"},
                    {"step": 1004, "message": f"   • Tüm mailler zaten işlenmiş, yeni mail yok.", "status": "info"},
                    {"step": 1005, "message": f"   • Toplam süre: {total_time:.2f} saniye", "status": "info"}
                ]
            else:
                summary_steps = [
                    {"step": 999, "message": "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "status": "info"},
                    {"step": 1000, "message": f"📊 İşleme Tamamlandı", "status": "success"},
                    {"step": 1001, "message": f"   • Toplam mail: {total_fetched}", "status": "info"},
                    {"step": 1002, "message": f"   • İşlenen: {processed_count}", "status": "success"},
                    {"step": 1003, "message": f"   • Atlanan (duplicate): {skipped_count}", "status": "skip"},
                    {"step": 1004, "message": f"   • Hata: {len(errors)}", "status": "error" if len(errors) > 0 else "info"},
                    {"step": 1005, "message": f"   • Toplam süre: {total_time:.2f} saniye", "status": "info"},
                    {"step": 1006, "message": f"   • Ortalama işleme süresi: {avg_processing_time:.2f} saniye/mail", "status": "info"}
                ]

            for step in summary_steps:
                yield f"data: {json.dumps(step)}\n\n"
                await asyncio.sleep(0.01)
            
            print(f"\n📊 Mail İşleme Özeti (SSE Akışı Tamamlandı):")
            print(f"   - Toplam mail: {total_fetched}")
            print(f"   - İşlenen: {processed_count}")
            # ... (diğer print logları)
            
        except imaplib.IMAP4.error as e:
            error_detail = f"IMAP bağlantı hatası: {str(e)}. Lütfen mail ayarlarınızı kontrol edin."
            print(f"Mail çekme IMAP hatası: {e}")
            step_data = {"step": 999, "message": f"❌ IMAP bağlantı hatası: {str(e)}", "status": "error"}
            yield f"data: {json.dumps(step_data)}\n\n"
        
        except Exception as e:
            import traceback
            error_detail = f"Mail çekme hatası: {str(e)}"
            print(f"Mail çekme hatası: {e}\n{traceback.format_exc()}")
            step_data = {"step": 999, "message": f"❌ Genel hata: {str(e)}", "status": "error"}
            yield f"data: {json.dumps(step_data)}\n\n"
    
    # --- Ana Fonksiyonun Dönüşü ---
    # Generator'ı çağır ve StreamingResponse olarak frontend'e döndür.
    return StreamingResponse(event_stream_generator(), media_type="text/event-stream")
# --- /fetch endpoint GÜNCELLEMESİ BİTTİ ---

@router.get("/live-content/{message_id}")
async def get_mail_live_content(
    message_id: str,
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """
    Mail body içeriğini IMAP'ten on-demand olarak çeker.
    Mail DB'de body=None olarak kaydedilmişse, bu endpoint kullanılır.
    """
    from app.services.mail_service import fetch_single_mail_body
    
    firestore_db = firestore.Client()
    mail_col = get_mail_collection(firestore_db)
    
    # Mail kaydını bul
    mail_query = (
        mail_col.where(filter=FieldFilter("tenant_id", "==", current_user.tenant_id))
        .where(filter=FieldFilter("message_id", "==", message_id))
        .limit(1)
    )
    mail_docs = list(mail_query.stream())
    
    if not mail_docs:
        raise HTTPException(status_code=404, detail="Mail bulunamadı")
    
    mail_data = mail_docs[0].to_dict()
    
    # Mail ayarlarını al
    settings_doc = firestore_db.collection("mail_settings").document(current_user.tenant_id).get()
    if not settings_doc.exists:
        raise HTTPException(status_code=404, detail="Mail ayarları bulunamadı")
    
    settings = settings_doc.to_dict()
    email_address = settings.get("email_address", "")
    password = settings.get("password", "")
    imap_server = settings.get("imap_server", "")
    imap_port = settings.get("imap_port", 993)
    
    if not email_address or not password:
        raise HTTPException(status_code=400, detail="Mail ayarları eksik")
    
    # IMAP'ten body'yi çek
    body_content = fetch_single_mail_body(
        email_address=email_address,
        password=password,
        imap_server=imap_server,
        imap_port=imap_port,
        message_id=message_id
    )
    
    if body_content is None:
        raise HTTPException(status_code=404, detail="Mail içeriği IMAP'ten çekilemedi")
    
    return {
        "message_id": message_id,
        "body": body_content
    }

# --- Kalan endpoint'ler (process, get_mail_summaries, get_mail_stats, query_mails, vb.) ---
# --- Bu fonksiyonlarda bir değişiklik yapılmadı. ---

@router.post("/process", response_model=MailSummary)
async def process_mail(
    sender: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    received_at: Optional[datetime] = Form(None),
    attachments: List[UploadFile] = File([]),
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository),
    storage: BaseStorageAdapter = Depends(get_storage_adapter)
):
    """Gelen maili işler, özetini çıkarır ve ekleri kaydeder."""
    firestore_db = firestore.Client()
    mail_col = get_mail_collection(firestore_db)
    
    # LLM ile mail işleme
    processed = process_mail_with_llm(body, subject, sender)
    
    # Mail kaydı oluştur
    mail_data = {
        "tenant_id": current_user.tenant_id,
        "sender": sender,
        "subject": subject,
        "body": body,
        "summary": processed.get("summary", ""),
        "is_critical": processed.get("is_critical", False),
        "is_answered": False,
        "attachments": [],
        "potential_tasks": processed.get("potential_tasks", []),
        "critical_dates": processed.get("critical_dates", {}),
        "created_at": datetime.now(),
        "received_at": received_at or datetime.now()
    }
    
    doc_ref = mail_col.document()
    mail_id = doc_ref.id
    mail_data["id"] = mail_id
    doc_ref.set(mail_data)
    
    # Ekleri kaydet
    if attachments:
        saved_paths = await save_mail_attachments(attachments, current_user.tenant_id, mail_id, storage)
        doc_ref.update({"attachments": saved_paths})
        mail_data["attachments"] = saved_paths
    
    # Mail içeriğini vektör veritabanına ekle (soru-cevap için)
    try:
        embedding_model = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004", google_api_key=GEMINI_API_KEY)
        mail_text = f"Gönderen: {sender}\nKonu: {subject}\nİçerik: {body}"
        embedding = embedding_model.embed_documents([mail_text])[0]
        if embedding:
            chunk_data = {
                "tenant_id": current_user.tenant_id,
                "file_id": f"mail_{mail_id}",
                "file_name": f"Mail: {subject}",
                "chunk_number": 0,
                "chunk_text": mail_text,
                "embedding": embedding,
                "mail_id": mail_id
            }
            db.add_text_chunks_batch([chunk_data])
    except Exception as e:
        print(f"Mail vektör ekleme hatası: {e}")
    
    return MailSummary(**mail_data)

@router.get("/summaries", response_model=List[MailSummary])
def get_mail_summaries(
    period: str = "daily",  # "daily", "weekly", "custom"
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """Mail özetlerini getirir."""
    firestore_db = firestore.Client()
    mail_col = get_mail_collection(firestore_db)
    
    query = mail_col.where(filter=FieldFilter("tenant_id", "==", current_user.tenant_id))
    
    # UTC+3 (Türkiye saati) kullanarak bugünün başlangıcını hesapla
    if period == "daily":
        start = get_today_start_utc3()
        print(f"📅 Günlük özet - Bugünün başlangıcı (UTC+3'e göre): {start}")
        query = query.where(filter=FieldFilter("received_at", ">=", start))
    elif period == "weekly":
        now_utc3 = get_now_utc3()
        start = now_utc3 - timedelta(days=7)
        print(f"📅 Haftalık özet - 7 gün öncesi (UTC+3'e göre): {start}")
        query = query.where(filter=FieldFilter("received_at", ">=", start))
    elif period == "custom" and start_date and end_date:
        try:
            # String tarihleri datetime'a çevir (YYYY-MM-DD formatı)
            if isinstance(start_date, str):
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            else:
                start_dt = start_date
            if isinstance(end_date, str):
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                # Bitiş tarihine günün sonunu ekle (23:59:59)
                end_dt = end_dt.replace(hour=23, minute=59, second=59)
            else:
                end_dt = end_date
            print(f"📅 Custom tarih filtresi: {start_dt} - {end_dt}")
            query = query.where(filter=FieldFilter("received_at", ">=", start_dt))
            query = query.where(filter=FieldFilter("received_at", "<=", end_dt))
        except Exception as e:
            print(f"❌ Tarih parse hatası: {e}")
            raise HTTPException(status_code=400, detail=f"Geçersiz tarih formatı: {str(e)}")
    
    # Firestore'da order_by kullanırken, önce where filtreleri, sonra order_by gelmeli
    # Ayrıca composite index gerekebilir, ama önce deneyelim
    try:
        query = query.order_by("received_at", direction=firestore.Query.DESCENDING).limit(50)
    except Exception as e:
        # Eğer composite index yoksa, order_by olmadan dene
        print(f"⚠️ Order_by hatası (composite index gerekebilir): {e}")
        query = query.limit(50)
    
    # Tüm mailleri topla ve thread'lere göre grupla
    all_mails = []
    for doc in query.stream():
        data = doc.to_dict()
        # Tarih kontrolü - Firestore timestamp'ini datetime'a çevir
        received_at = data.get("received_at")
        if received_at:
            # Firestore timestamp ise datetime'a çevir
            if hasattr(received_at, 'timestamp'):
                received_at = datetime.fromtimestamp(received_at.timestamp())
            elif not isinstance(received_at, datetime):
                # String veya başka bir format ise parse et
                try:
                    if isinstance(received_at, str):
                        received_at = datetime.fromisoformat(received_at.replace('Z', '+00:00'))
                    else:
                        received_at = datetime.now()
                except:
                    received_at = datetime.now()
            
            # Custom period için tarih kontrolü (ekstra güvenlik)
            if period == "custom" and start_date and end_date:
                try:
                    if isinstance(start_date, str):
                        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                    else:
                        start_dt = start_date
                    if isinstance(end_date, str):
                        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                        end_dt = end_dt.replace(hour=23, minute=59, second=59)
                    else:
                        end_dt = end_date
                    
                    # Tarih aralığında değilse atla
                    if received_at < start_dt or received_at > end_dt:
                        continue
                except Exception as e:
                    print(f"⚠️ Tarih kontrolü hatası: {e}")
        
        data["id"] = doc.id
        all_mails.append(data)
    
    # Thread'lere göre grupla
    threads = {}  # {thread_id: [mail1, mail2, ...]}
    for mail in all_mails:
        thread_id = mail.get("thread_id") or mail.get("message_id") or mail.get("id")
        if thread_id not in threads:
            threads[thread_id] = []
        threads[thread_id].append(mail)
    
    # Her thread için özet oluştur
    result_mails = []
    for thread_id, thread_mails in threads.items():
        if len(thread_mails) == 1:
            # Tek mail ise direkt ekle
            result_mails.append(MailSummary(**thread_mails[0]))
        else:
            # Birden fazla mail varsa thread özeti oluştur
            print(f"🔗 Thread özeti oluşturuluyor: {thread_id} ({len(thread_mails)} mail)")
            try:
                # Thread özeti oluştur
                thread_summary = process_thread_with_llm(thread_mails)
                
                # En son mailin bilgilerini kullan (thread'in temsilcisi olarak)
                def get_received_at(mail):
                    received_at = mail.get("received_at", datetime.now())
                    if isinstance(received_at, str):
                        try:
                            return datetime.fromisoformat(received_at.replace('Z', '+00:00'))
                        except:
                            return datetime.now()
                    elif hasattr(received_at, 'timestamp'):
                        return datetime.fromtimestamp(received_at.timestamp())
                    return received_at if isinstance(received_at, datetime) else datetime.now()
                
                latest_mail = max(thread_mails, key=get_received_at)
                
                # Thread özeti ile birleştirilmiş mail oluştur
                latest_body = latest_mail.get("body") or ""
                thread_mail_data = {
                    "id": thread_id,  # Thread ID'yi mail ID olarak kullan
                    "tenant_id": latest_mail.get("tenant_id"),
                    "sender": latest_mail.get("sender"),
                    "subject": latest_mail.get("subject", ""),
                    "body": f"[Thread: {len(thread_mails)} mail] " + latest_body if latest_body else None,
                    "has_full_content": latest_mail.get("has_full_content", False),
                    "summary": thread_summary.get("summary", ""),
                    "is_critical": thread_summary.get("is_critical", False),
                    "is_answered": latest_mail.get("is_answered", False),
                    "is_read": all(m.get("is_read", False) for m in thread_mails),  # Tümü okunmuşsa okunmuş
                    "attachments": [],  # Thread'teki tüm attachment'ları birleştir
                    "attachment_summaries": {},  # Thread'teki tüm attachment özetlerini birleştir
                    "attachment_tables": {},  # Thread'teki tüm tablo verilerini birleştir
                    "potential_tasks": thread_summary.get("potential_tasks", []),
                    "critical_dates": thread_summary.get("critical_dates", {}),
                    "created_at": latest_mail.get("created_at", datetime.now()),
                    "received_at": max(get_received_at(m) for m in thread_mails)  # En son mailin tarihi
                }
                
                # Attachment'ları birleştir
                for mail in thread_mails:
                    thread_mail_data["attachments"].extend(mail.get("attachments", []))
                    thread_mail_data["attachment_summaries"].update(mail.get("attachment_summaries", {}))
                    thread_mail_data["attachment_tables"].update(mail.get("attachment_tables", {}))
                
                result_mails.append(MailSummary(**thread_mail_data))
                print(f"✅ Thread özeti oluşturuldu: {thread_id}")
            except Exception as e:
                print(f"⚠️ Thread özeti oluşturma hatası ({thread_id}): {e}")
                # Hata durumunda en son maili ekle
                result_mails.append(MailSummary(**max(thread_mails, key=lambda x: x.get("received_at", datetime.now()))))
    
    # Tarihe göre sırala (en yeni önce)
    result_mails.sort(key=lambda x: x.received_at if isinstance(x.received_at, datetime) else datetime.now(), reverse=True)
    
    print(f"📊 {len(all_mails)} mail, {len(threads)} thread bulundu (period: {period}, custom: {period == 'custom'})")
    return result_mails[:50]  # En fazla 50 thread göster

@router.get("/stats", response_model=MailStats)
def get_mail_stats(
    period: str = "daily",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """Mail istatistiklerini getirir."""
    firestore_db = firestore.Client()
    mail_col = get_mail_collection(firestore_db)
    
    query = mail_col.where(filter=FieldFilter("tenant_id", "==", current_user.tenant_id))
    
    # UTC+3 (Türkiye saati) kullanarak tarih filtreleme
    if period == "daily":
        start = get_today_start_utc3()
        print(f"📅 Mail istatistikleri - Günlük özet - Bugünün başlangıcı (UTC+3'e göre): {start}")
        query = query.where(filter=FieldFilter("received_at", ">=", start))
    elif period == "weekly":
        now_utc3 = get_now_utc3()
        start = now_utc3 - timedelta(days=7)
        print(f"📅 Mail istatistikleri - Haftalık özet - 7 gün öncesi (UTC+3'e göre): {start}")
        query = query.where(filter=FieldFilter("received_at", ">=", start))
    elif period == "custom" and start_date and end_date:
        try:
            if isinstance(start_date, str):
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            else:
                start_dt = start_date
            if isinstance(end_date, str):
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                end_dt = end_dt.replace(hour=23, minute=59, second=59)
            else:
                end_dt = end_date
            query = query.where(filter=FieldFilter("received_at", ">=", start_dt))
            query = query.where(filter=FieldFilter("received_at", "<=", end_dt))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Geçersiz tarih formatı: {str(e)}")
    
    total = critical = unanswered = unread = 0
    for doc in query.stream():
        data = doc.to_dict()
        total += 1
        if data.get("is_critical"): critical += 1
        if not data.get("is_answered"): unanswered += 1
        # Okunmamış mail: is_read field'ı yoksa veya false ise okunmamış sayılır
        if not data.get("is_read", False): unread += 1
    
    return MailStats(total_mails=total, critical_mails=critical, unanswered_mails=unanswered, unread_mails=unread)

@router.post("/query")
def query_mails(
    request: MailQueryRequest,
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """Mailler içinde soru-cevap arama yapar."""
    
    # Selamlaşma/hal hatır kontrolü (en önce - nazik cevap verilmeli)
    if is_greeting_query(request.query):
        print(f"👋 Selamlaşma mail sorgusu tespit edildi: '{request.query}' - Nazik cevap verilecek")
        greeting_response = get_greeting_response(request.query)
        return {
            "answer": greeting_response,
            "mails": []
        }
    
    # Off-topic sorgu kontrolü (mail taraması yapmadan önce)
    if is_off_topic_query(request.query):
        print(f"⚠️ Off-topic mail sorgusu tespit edildi: '{request.query}' - Mail taraması yapılmayacak")
        return {
            "answer": "Üzgünüm, bu tür genel sohbet sorularını yanıtlayamam. Lütfen mail içerikleri ile ilgili sorular sorun. Örneğin: 'Bugün kaç mail geldi?', 'Kritik mailleri listele', 'X konusunda gelen mailleri göster' gibi.",
            "mails": []
        }
    
    # Yardım/destek sorgu kontrolü (mail taraması yapmadan önce)
    if is_help_or_support_query(request.query):
        print(f"ℹ️ Yardım/destek mail sorgusu tespit edildi: '{request.query}' - Mail taraması yapılmayacak, direkt cevap verilecek")
        help_response = get_help_response(request.query)
        return {
            "answer": help_response,
            "mails": []
        }
    
    firestore_db = firestore.Client()
    mail_col = get_mail_collection(firestore_db)
    
    # Soruyu analiz et ve otomatik tarih filtresi uygula
    query_lower_for_date = request.query.lower()
    today_keywords = ['bugün', 'today', 'bugünkü', 'bugünün', 'bugüne', 'bugünde']
    week_keywords = ['bu hafta', 'this week', 'haftalık', 'weekly']
    month_keywords = ['bu ay', 'this month', 'aylık', 'monthly']
    one_month_keywords = ['1 ay', 'bir ay', 'one month', '1 month', 'son 1 ay', 'son bir ay', 'last month']
    
    # Eğer soru tarih içeriyorsa ve date_range "all" ise, otomatik olarak tarih filtresi uygula
    auto_date_range = None
    if request.date_range == "all" or not request.date_range:
        if any(keyword in query_lower_for_date for keyword in today_keywords):
            auto_date_range = "daily"
            print(f"🔍 Soru analizi: 'bugün' kelimesi tespit edildi, otomatik 'daily' filtresi uygulanıyor")
        elif any(keyword in query_lower_for_date for keyword in week_keywords):
            auto_date_range = "weekly"
            print(f"🔍 Soru analizi: 'bu hafta' kelimesi tespit edildi, otomatik 'weekly' filtresi uygulanıyor")
        elif any(keyword in query_lower_for_date for keyword in one_month_keywords):
            auto_date_range = "one_month"
            print(f"🔍 Soru analizi: '1 ay' kelimesi tespit edildi, otomatik 'one_month' filtresi uygulanıyor")
        elif any(keyword in query_lower_for_date for keyword in month_keywords):
            auto_date_range = "monthly"
            print(f"🔍 Soru analizi: 'bu ay' kelimesi tespit edildi, otomatik 'monthly' filtresi uygulanıyor")
    
    # Tarih filtresi için tarih aralığını hesapla (UTC+3 timezone'a göre)
    date_filter_start = None
    date_filter_end = None
    effective_date_range = auto_date_range if auto_date_range else request.date_range
    if effective_date_range and effective_date_range != "all":
        now_utc3 = datetime.now(TURKEY_TIMEZONE)
        now_utc = get_now_utc3()  # UTC'ye çevrilmiş şu anki zaman
        
        if effective_date_range == "daily":
            # Bugünün başlangıcı (UTC+3'te 00:00:00, UTC'ye çevrilmiş)
            date_filter_start = get_today_start_utc3()
            date_filter_end = datetime(now_utc3.year, now_utc3.month, now_utc3.day, 23, 59, 59, 999999)
        elif effective_date_range == "weekly":
            # Son 7 gün
            date_filter_start = now_utc - timedelta(days=7)
            date_filter_end = now_utc  # Şu ana kadar
        elif effective_date_range == "one_month":
            # Son 1 ay (30 gün)
            date_filter_start = now_utc - timedelta(days=30)
            date_filter_end = now_utc  # Şu ana kadar
        elif effective_date_range == "monthly":
            # Bu ayın başı (UTC+3'te)
            month_start_utc3 = datetime(now_utc3.year, now_utc3.month, 1, tzinfo=TURKEY_TIMEZONE)
            date_filter_start = month_start_utc3.astimezone(timezone.utc).replace(tzinfo=None)
            date_filter_end = now_utc  # Şu ana kadar
        elif effective_date_range == "custom" and request.start_date and request.end_date:
            try:
                if isinstance(request.start_date, str):
                    date_filter_start = datetime.strptime(request.start_date, "%Y-%m-%d")
                else:
                    date_filter_start = request.start_date
                if isinstance(request.end_date, str):
                    date_filter_end = datetime.strptime(request.end_date, "%Y-%m-%d")
                    date_filter_end = date_filter_end.replace(hour=23, minute=59, second=59)
                else:
                    date_filter_end = request.end_date
            except Exception as e:
                print(f"⚠️ Tarih parse hatası: {e}")
    
    # Soru içinde "ek", "attachment", "dosya" gibi kelimeler varsa direkt Firestore'dan arama yap
    query_lower = query_lower_for_date
    attachment_keywords = ['ek', 'attachment', 'dosya', 'file', 'ekli', 'ekte', 'ekler']
    is_attachment_query = any(keyword in query_lower for keyword in attachment_keywords)
    
    # İstatistik sorularını tespit et (sender istatistiği, en çok, en az, toplam vb.)
    statistics_keywords = ['en çok', 'en az', 'en fazla', 'en az', 'toplam', 'kaç', 'kimden', 'gönderen', 'sender', 
                          'istatistik', 'statistics', 'sayı', 'count', 'hangi', 'kim', 'en yüksek', 'en düşük']
    sender_statistics_keywords = ['kimden', 'gönderen', 'sender', 'kim', 'hangi kişi', 'hangi kişiden', 
                                 'en çok mail', 'en fazla mail', 'mail sayısı', 'mail count']
    is_statistics_query = any(keyword in query_lower for keyword in statistics_keywords)
    is_sender_statistics_query = is_statistics_query and any(keyword in query_lower for keyword in sender_statistics_keywords)
    
    if is_sender_statistics_query:
        print(f"📊 Sender istatistik sorgusu tespit edildi: '{request.query}' - Tüm mailler analiz edilecek")
    
    # Soru içinde "kritik" kelimesi varsa kritik mail filtresi uygula
    # Yazım hatalarına tolerans için fuzzy matching
    import re
    critical_keywords = ['kritik', 'critical', 'acil', 'urgent', 'önemli', 'important']
    # Yaygın yazım hataları - direkt kontrol
    critical_typos = ['ktirik', 'kirtik', 'kritik', 'kritik', 'kritik', 'kritik']
    
    is_critical_query = any(keyword in query_lower for keyword in critical_keywords)
    
    # Yazım hatası kontrolü: "ktirik", "kirtik" gibi yaygın hataları da kontrol et
    if not is_critical_query:
        # "ktirik" gibi yazım hatalarını kontrol et (kritik kelimesinin harfleri karışmış)
        # Basit kontrol: "k" ile başlayan ve "t", "r", "i", "k" harflerini içeren 5-6 harfli kelimeler
        words = query_lower.split()
        for word in words:
            # "ktirik" gibi kelimeleri yakala (k ile başlayan, t, r, i, k içeren, 5-6 harfli)
            word_clean = word.strip('.,!?;:')
            if len(word_clean) >= 5 and len(word_clean) <= 6:
                if word_clean.startswith('k') and 't' in word_clean and 'r' in word_clean and 'i' in word_clean and word_clean.endswith('k'):
                    # "kritik" kelimesinin harflerini içeriyor mu kontrol et
                    word_chars = set(word_clean)
                    kritik_chars = set('kritik')
                    if len(word_chars & kritik_chars) >= 4:  # En az 4 harf eşleşiyorsa
                        is_critical_query = True
                        print(f"🔍 Kritik mail sorgusu tespit edildi: Yazım hatası düzeltildi ('{word_clean}' -> 'kritik')")
                        break
    
    if is_critical_query:
        matched_keyword = [kw for kw in critical_keywords if kw in query_lower]
        if matched_keyword:
            print(f"🔍 Kritik mail sorgusu tespit edildi: '{matched_keyword[0]}' kelimesi bulundu")
        else:
            print(f"🔍 Kritik mail sorgusu tespit edildi: Yazım hatası düzeltildi")
    else:
        print(f"🔍 Kritik mail sorgusu tespit edilmedi. Soru: '{request.query}' (lowercase: '{query_lower}')")
    
    mail_ids = set()
    results = []
    
    # Önce tarih filtresine uyan mail ID'lerini topla (tarih filtresi varsa)
    # Eğer kritik mail sorgusu varsa, kritik mail filtresini de ekle
    filtered_mail_ids = None
    if date_filter_start or date_filter_end or is_critical_query:
        print(f"📅 Tarih filtresi uygulanıyor: {date_filter_start} - {date_filter_end}")
        if is_critical_query:
            print(f"🔴 Kritik mail filtresi uygulanıyor")
        
        date_query = mail_col.where(filter=FieldFilter("tenant_id", "==", current_user.tenant_id))
        if date_filter_start:
            date_query = date_query.where(filter=FieldFilter("received_at", ">=", date_filter_start))
        if date_filter_end:
            date_query = date_query.where(filter=FieldFilter("received_at", "<=", date_filter_end))
        if is_critical_query:
            date_query = date_query.where(filter=FieldFilter("is_critical", "==", True))
        
        filtered_mail_ids = set()
        critical_count_in_filter = 0
        for doc in date_query.stream():
            filtered_mail_ids.add(doc.id)
            # Kritik mail sayısını kontrol et
            if is_critical_query:
                data = doc.to_dict()
                if data.get("is_critical", False):
                    critical_count_in_filter += 1
        print(f"📊 Tarih/kritik filtresine uyan {len(filtered_mail_ids)} mail bulundu")
        if is_critical_query:
            print(f"🔴 Filtrelenmiş maillerden {critical_count_in_filter} tanesi kritik (is_critical=True)")
    
    if is_attachment_query:
        # Direkt Firestore'dan attachment'ı olan mailleri getir
        print(f"📎 Attachment sorgusu tespit edildi, Firestore'dan direkt arama yapılıyor...")
        query = mail_col.where(filter=FieldFilter("tenant_id", "==", current_user.tenant_id))
        
        # Tarih filtresi ekle
        if date_filter_start:
            query = query.where(filter=FieldFilter("received_at", ">=", date_filter_start))
        if date_filter_end:
            query = query.where(filter=FieldFilter("received_at", "<=", date_filter_end))
        
        # Limit uygula (çok fazla sonuç olmasın)
        try:
            query = query.limit(100)
        except:
            pass
        
        # Kritik mail filtresi ekle
        if is_critical_query:
            query = query.where(filter=FieldFilter("is_critical", "==", True))
        
        # Attachment'ı olan mailleri bul
        for doc in query.stream():
            data = doc.to_dict()
            attachments = data.get("attachments", [])
            if attachments and len(attachments) > 0:
                # Kritik mail sorgusu varsa, sadece kritik mailleri ekle
                if is_critical_query:
                    if data.get("is_critical", False):
                        mail_ids.add(doc.id)
                else:
                    mail_ids.add(doc.id)
    elif is_sender_statistics_query:
        # Sender istatistik sorgusu için tüm mailleri Firestore'dan çek
        print(f"📊 Sender istatistik sorgusu - Tüm mailler Firestore'dan çekiliyor...")
        stats_query = mail_col.where(filter=FieldFilter("tenant_id", "==", current_user.tenant_id))
        
        # Tarih filtresi ekle
        if date_filter_start:
            stats_query = stats_query.where(filter=FieldFilter("received_at", ">=", date_filter_start))
        if date_filter_end:
            stats_query = stats_query.where(filter=FieldFilter("received_at", "<=", date_filter_end))
        
        # Tüm mailleri çek
        for doc in stats_query.stream():
            mail_ids.add(doc.id)
        print(f"📊 Sender istatistik için {len(mail_ids)} mail bulundu")
    else:
        # Normal vektör araması
        # Eğer kritik mail sorgusu varsa ve tarih filtresi varsa, direkt Firestore'dan çek (daha hızlı ve doğru)
        if is_critical_query and filtered_mail_ids:
            print(f"🔴 Kritik mail sorgusu + tarih filtresi tespit edildi, direkt Firestore'dan çekiliyor...")
            # filtered_mail_ids zaten kritik ve tarih filtresine uyan mailleri içeriyor
            mail_ids.update(filtered_mail_ids)
            print(f"📊 Firestore'dan {len(filtered_mail_ids)} kritik mail ID'si mail_ids set'ine eklendi (toplam mail_ids: {len(mail_ids)})")
        elif filtered_mail_ids and (any(keyword in query_lower_for_date for keyword in ['bugün', 'today', 'bugünkü', 'bugünün', 'bugüne', 'bugünde', 'özet', 'özetle', 'özetler']) or len(request.query.split()) <= 5):
            # Eğer tarih filtresi varsa ve sorgu "bugün" gibi genel bir sorguysa veya çok kısa bir sorguysa,
            # vektör araması yapmadan direkt tarih filtresine uyan mailleri getir
            print(f"📅 Tarih filtresi + genel sorgu tespit edildi, direkt Firestore'dan çekiliyor...")
            mail_ids.update(filtered_mail_ids)
            print(f"📊 Firestore'dan {len(filtered_mail_ids)} mail ID'si mail_ids set'ine eklendi (toplam mail_ids: {len(mail_ids)})")
        else:
            # Vektör araması yap
            chunks = vector_service.search_similar_chunks(
                tenant_id=current_user.tenant_id,
                query=request.query,
                db=db,
                limit=50,
                filter_file_ids=None
            )
            
            # Mail ID'lerini topla (file_id'den mail_id çıkar)
            # Eğer tarih/kritik filtresi varsa, sadece filtrelenmiş mailleri dahil et
            print(f"🔍 Vektör araması: {len(chunks)} chunk bulundu")
            print(f"🔍 Kritik mail sorgusu: {is_critical_query}, filtered_mail_ids: {len(filtered_mail_ids) if filtered_mail_ids else 'None'}")
            vector_mail_count = 0
            for chunk in chunks:
                file_id = chunk.get("source_file_id", "")
                if file_id.startswith("mail_"):
                    mail_id = file_id.replace("mail_", "")
                    # Tarih filtresi veya kritik mail filtresi varsa, sadece filtrelenmiş mailleri dahil et
                    if filtered_mail_ids is None:
                        # Filtre yoksa, tüm vektör araması sonuçlarını ekle
                        mail_ids.add(mail_id)
                        vector_mail_count += 1
                    elif mail_id in filtered_mail_ids:
                        # Filtre varsa, sadece filtrelenmiş mailleri ekle
                        mail_ids.add(mail_id)
                        vector_mail_count += 1
                    else:
                        # Filtre var ama bu mail filtrelenmiş set'te yok
                        if is_critical_query:
                            print(f"⚠️ Vektör araması sonucu {mail_id} kritik mail filtresine uymuyor, eklenmedi")
            
            # Eğer vektör araması sonucu yoksa ama tarih filtresi varsa, direkt tarih filtresine uyan mailleri kullan
            if vector_mail_count == 0 and filtered_mail_ids and len(filtered_mail_ids) > 0:
                print(f"⚠️ Vektör araması sonucu bulunamadı, tarih filtresine uyan {len(filtered_mail_ids)} mail kullanılıyor...")
                mail_ids.update(filtered_mail_ids)
            
            print(f"📊 Vektör aramasından {vector_mail_count} mail mail_ids set'ine eklendi (toplam mail_ids: {len(mail_ids)})")
        
        # NOT: Tarih filtresi varsa, sadece vektör araması sonuçlarını kullan
        # Tarih filtresine uyan tüm mailleri eklemek yanlış sonuçlara yol açar
        # (örn: "bugün kaç mail geldi" sorusunda bugün olmayan mailleri de gösterir)
    
    # Mail detaylarını getir ve filtrele (tarih ve kritik mail filtresi)
    print(f"📋 {len(mail_ids)} mail ID'si için detay çekiliyor...")
    filtered_out_count = 0
    for mail_id in list(mail_ids):
        doc = mail_col.document(mail_id).get()
        if doc.exists:
            data = doc.to_dict()
            if data.get("tenant_id") != current_user.tenant_id:
                filtered_out_count += 1
                continue
            
            # received_at timestamp'ini datetime'a çevir
            received_at = None
            if "received_at" in data:
                received_at_raw = data["received_at"]
                if hasattr(received_at_raw, 'timestamp'):
                    received_at = datetime.fromtimestamp(received_at_raw.timestamp())
                elif isinstance(received_at_raw, str):
                    try:
                        received_at = datetime.fromisoformat(received_at_raw.replace('Z', '+00:00'))
                    except:
                        pass
                elif isinstance(received_at_raw, datetime):
                    received_at = received_at_raw
            
            # Tarih filtresi doğrulama (zaten filtrelenmiş olmalı ama güvenlik için)
            # received_at None ise de filtrele (tarih bilgisi olmayan mailleri hariç tut)
            if date_filter_start:
                if not received_at or received_at < date_filter_start:
                    filtered_out_count += 1
                    print(f"⚠️ Mail {mail_id} tarih filtresine uymuyor (received_at={received_at}, filter_start={date_filter_start})")
                    continue
            if date_filter_end:
                if not received_at or received_at > date_filter_end:
                    filtered_out_count += 1
                    print(f"⚠️ Mail {mail_id} tarih filtresine uymuyor (received_at={received_at}, filter_end={date_filter_end})")
                    continue
            
            # Kritik mail filtresi doğrulama (MUTLAKA uygulanmalı)
            if is_critical_query:
                is_critical = data.get("is_critical", False)
                # Farklı formatları kontrol et (bool, string, int)
                if isinstance(is_critical, str):
                    is_critical = is_critical.lower() in ['true', '1', 'yes', 'evet']
                elif isinstance(is_critical, int):
                    is_critical = bool(is_critical)
                
                if not is_critical:
                    filtered_out_count += 1
                    print(f"⚠️ Mail {mail_id} kritik değil, filtreleniyor (is_critical={data.get('is_critical')} -> {is_critical})")
                    continue
            
            data["id"] = doc.id
            data["received_at"] = received_at
            results.append(MailSummary(**data))
    
    if filtered_out_count > 0:
        print(f"🚫 {filtered_out_count} mail filtrelendi, {len(results)} mail sonuç olarak döndürülüyor")
    
    # Kritik mail sorgusu varsa, sonuçları bir kez daha filtrele (güvenlik için)
    if is_critical_query:
        original_count = len(results)
        results = [m for m in results if m.is_critical]
        if len(results) != original_count:
            print(f"🔴 Kritik mail filtresi: {original_count} mail'den {len(results)} kritik mail kaldı ({original_count - len(results)} mail filtrelendi)")
    
    print(f"✅ Toplam {len(results)} mail sonuç olarak döndürülüyor")
    
    # LLM ile cevap oluştur
    llm = get_llm_for_model("gemini")
    
    # Tarih aralığı bilgisini hazırla
    date_range_info = ""
    if date_filter_start and date_filter_end:
        months_tr = {
            1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan",
            5: "Mayıs", 6: "Haziran", 7: "Temmuz", 8: "Ağustos",
            9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık"
        }
        start_str = f"{date_filter_start.day} {months_tr.get(date_filter_start.month, date_filter_start.strftime('%B'))} {date_filter_start.year}"
        end_str = f"{date_filter_end.day} {months_tr.get(date_filter_end.month, date_filter_end.strftime('%B'))} {date_filter_end.year}"
        if start_str == end_str:
            date_range_info = f"Tarih aralığı: {start_str}"
        else:
            date_range_info = f"Tarih aralığı: {start_str} - {end_str}"
    elif date_filter_start:
        months_tr = {
            1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan",
            5: "Mayıs", 6: "Haziran", 7: "Temmuz", 8: "Ağustos",
            9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık"
        }
        start_str = f"{date_filter_start.day} {months_tr.get(date_filter_start.month, date_filter_start.strftime('%B'))} {date_filter_start.year}"
        date_range_info = f"Tarih aralığı: {start_str} ve sonrası"
    
    # Mail tarihlerini de context'e ekle (tüm sonuçları ekle, sadece ilk 5'i değil)
    context_parts = []
    for m in results:
        # Tarih bilgisini formatla
        received_date = m.received_at
        if received_date:
            if isinstance(received_date, datetime):
                # Türkçe ay isimleri
                months_tr = {
                    1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan",
                    5: "Mayıs", 6: "Haziran", 7: "Temmuz", 8: "Ağustos",
                    9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık"
                }
                date_str = f"{received_date.day} {months_tr.get(received_date.month, received_date.strftime('%B'))} {received_date.year}, {received_date.strftime('%H:%M')}"
            elif isinstance(received_date, str):
                try:
                    dt = datetime.fromisoformat(received_date.replace('Z', '+00:00'))
                    months_tr = {
                        1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan",
                        5: "Mayıs", 6: "Haziran", 7: "Temmuz", 8: "Ağustos",
                        9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık"
                    }
                    date_str = f"{dt.day} {months_tr.get(dt.month, dt.strftime('%B'))} {dt.year}, {dt.strftime('%H:%M')}"
                except:
                    date_str = received_date
            else:
                date_str = str(received_date)
        else:
            date_str = "Tarih bilgisi yok"
        
        # Kritik mail bilgisini ekle
        critical_mark = "🔴 KRİTİK" if m.is_critical else ""
        context_parts.append(f"📅 {date_str} - Gönderen: {m.sender}\nKonu: {m.subject}\nÖzet: {m.summary}{' ' + critical_mark if critical_mark else ''}")
    
    context = "\n\n".join(context_parts)
    
    # Kritik mail sayısını hesapla
    critical_count = sum(1 for m in results if m.is_critical)
    
    # Sender istatistik sorgusu için sender sayılarını hesapla
    sender_statistics = ""
    if is_sender_statistics_query and results:
        sender_counts = Counter(m.sender for m in results if m.sender)
        # En çok mail gönderenleri sırala
        top_senders = sender_counts.most_common(10)  # İlk 10 gönderen
        
        sender_statistics = "\n\nGÖNDEREN İSTATİSTİKLERİ:\n"
        for sender, count in top_senders:
            sender_statistics += f"- {sender}: {count} mail\n"
        
        print(f"📊 Sender istatistikleri hesaplandı: {len(sender_counts)} farklı gönderen, toplam {len(results)} mail")
    
    # Prompt'a toplam mail sayısı ve tarih aralığı bilgisini ekle
    total_count_info = f"Toplam {len(results)} mail bulundu."
    if critical_count > 0:
        total_count_info += f" Bunlardan {critical_count} tanesi kritik mail (is_critical=true)."
    if date_range_info:
        total_count_info += f" {date_range_info}."
    
    # Kritik mail sorgusu için özel bilgi ekle
    critical_info = ""
    if is_critical_query:
        critical_info = "\n\nÖNEMLİ: Soru 'kritik mail' hakkındaysa, sadece is_critical=true olan mailleri say. Mail özetlerinde '🔴 KRİTİK' işareti olan mailler kritik maillerdir."
    
    # Sender istatistik sorgusu için özel talimat
    sender_info = ""
    if is_sender_statistics_query:
        sender_info = "\n\nÖNEMLİ: Soru 'en çok mail kimden', 'hangi gönderen', 'sender istatistiği' gibi bir soruysa, yukarıdaki GÖNDEREN İSTATİSTİKLERİ bölümündeki bilgileri kullan. En çok mail gönderen kişiyi ve sayısını belirt."
    
    # Soru tipini tespit et: "var mı", "kaç" gibi sorular için sadece sayı, "listele", "göster" için detaylı liste
    query_lower_for_type = request.query.lower()
    is_count_only_query = any(keyword in query_lower_for_type for keyword in ['var mı', 'var mi', 'var mı?', 'var mi?', 'kaç tane', 'kaç adet', 'kaç mail', 'toplam kaç'])
    is_list_query = any(keyword in query_lower_for_type for keyword in ['listele', 'göster', 'hangi mailler', 'hangi mail', 'mailleri göster', 'mailleri listele', 'detay', 'detaylı'])
    
    # Sender istatistik sorgusu için context'i kısalt (çok fazla mail varsa)
    if is_sender_statistics_query and len(context_parts) > 50:
        # Sadece sender istatistiklerini göster, tüm mail listesini gösterme
        context_for_prompt = f"Toplam {len(results)} mail analiz edildi. Detaylı mail listesi çok uzun olduğu için gösterilmiyor.{sender_statistics}"
    elif is_count_only_query and not is_list_query:
        # "Var mı", "kaç tane" gibi sorular için sayı + ilgili maillerin özeti
        if len(results) > 0:
            # İlk birkaç mailin özetini göster (max 10 - daha fazla örnek göster)
            max_samples = min(10, len(results))
            sample_mails = context_parts[:max_samples]
            sample_text = "\n\n".join(sample_mails)
            if len(results) > max_samples:
                context_for_prompt = f"Toplam {len(results)} mail bulundu. İlk {max_samples} mail:\n\n{sample_text}\n\n(Not: Toplam {len(results)} mail var, ilk {max_samples} tanesi gösteriliyor)"
            else:
                context_for_prompt = f"Bulunan {len(results)} mail:\n\n{sample_text}"
        else:
            context_for_prompt = "Hiç mail bulunamadı."
    else:
        # "Listele", "göster" gibi sorular için tüm mail listesi
        context_for_prompt = context if context else "Hiç mail bulunamadı."
    
    # Soru tipine göre özel talimat
    query_type_instruction = ""
    if is_count_only_query and not is_list_query:
        query_type_instruction = f"\n\nÖNEMLİ: Soru 'var mı', 'kaç tane', 'kaç adet' gibi bir soruysa:\n1. Önce sayıyı ve kısa bir bilgi ver (örn: 'Evet, {len(results)} adet toplantı ile ilgili mail bulundu.' veya 'Hayır, toplantı ile ilgili mail bulunamadı.')\n2. Sonra bulunan maillerin kısa bir özetini listele. Her mail için gönderen ve konu bilgisini içer.\n3. Tüm mailleri detaylı olarak yazma, sadece özet bilgi ver."
    elif is_list_query:
        query_type_instruction = "\n\nÖNEMLİ: Soru 'listele', 'göster', 'hangi mailler' gibi bir soruysa, bulunan maillerin detaylı listesini ver. Her mail için gönderen, konu ve özet bilgisini içer."
    
    prompt = f"""Aşağıdaki mail özetlerine göre soruyu cevapla. {total_count_info}
Her mail için tarih bilgisi ve kritik mail durumu (is_critical) verilmiştir. Kritik mailler '🔴 KRİTİK' işaretiyle belirtilmiştir.
Eğer soru mail sayısı hakkındaysa, sadece verilen mail sayısını söyle.{critical_info}{sender_info}{query_type_instruction}

{sender_statistics if sender_statistics else ""}

{context_for_prompt}

Soru: {request.query}
Cevap:"""
    
    try:
        response = llm.invoke(prompt)
        answer = response.content if hasattr(response, 'content') else str(response)
        
        # LLM'in cevabından ilgili mailleri filtrele
        # Eğer LLM belirli sayıda mail belirtiyorsa, hangi maillerin ilgili olduğunu sor
        import re
        count_matches = re.findall(r'(\d+)\s*(?:adet|tane|mail)', answer.lower())
        
        if count_matches and len(results) > 0:
            llm_count = int(count_matches[0])
            # Eğer LLM'in belirttiği sayı, bulunan mail sayısından azsa, hangi maillerin ilgili olduğunu sor
            if llm_count < len(results) and llm_count > 0:
                print(f"🔍 LLM {llm_count} mail belirtti, {len(results)} mail bulundu. İlgili mailleri filtreliyorum...")
                
                # Hangi maillerin ilgili olduğunu belirlemek için ikinci bir LLM çağrısı
                mail_list_text = ""
                for i, mail in enumerate(results[:20], 1):  # İlk 20 maili göster (çok fazla olmasın)
                    mail_list_text += f"{i}. Konu: {mail.subject}\n   Gönderen: {mail.sender}\n\n"
                
                filter_prompt = f"""Aşağıdaki mail listesinden, kullanıcının sorusuna gerçekten ilgili olan mailleri seç.

KULLANICI SORUSU: "{request.query}"

LLM CEVABI: "{answer}"

MAIL LİSTESİ:
{mail_list_text}

GÖREV: LLM cevabında belirtilen sayıda mail ({llm_count} adet) gerçekten ilgili olan mailleri seç.

YANIT FORMATI: Sadece JSON formatında yanıt ver:
{{
    "relevant_mail_indices": [1, 3, 5]
}}

Sadece ilgili maillerin numaralarını (indices) listele. Örnek: Eğer 1, 3 ve 5 numaralı mailler ilgiliyse, [1, 3, 5] döndür.

YANIT:"""
                
                try:
                    filter_response = llm.invoke(filter_prompt)
                    filter_text = filter_response.content if hasattr(filter_response, 'content') else str(filter_response)
                    
                    # JSON'u parse et
                    if "```json" in filter_text:
                        json_start = filter_text.find("```json") + 7
                        json_end = filter_text.find("```", json_start)
                        json_str = filter_text[json_start:json_end].strip()
                    elif "```" in filter_text:
                        json_start = filter_text.find("```") + 3
                        json_end = filter_text.find("```", json_start)
                        json_str = filter_text[json_start:json_end].strip()
                    else:
                        json_str = filter_text.strip()
                    
                    if "{" in json_str and "}" in json_str:
                        json_start = json_str.find("{")
                        json_end = json_str.rfind("}") + 1
                        json_str = json_str[json_start:json_end]
                    
                    filter_result = json.loads(json_str)
                    relevant_indices = filter_result.get("relevant_mail_indices", [])
                    
                    # İndeksleri 0-based'e çevir (1-based'den)
                    relevant_indices_0based = [i - 1 for i in relevant_indices if 1 <= i <= len(results)]
                    
                    if relevant_indices_0based:
                        results = [results[i] for i in relevant_indices_0based if 0 <= i < len(results)]
                        print(f"✅ {len(results)} ilgili mail filtrelendi (LLM: {llm_count} mail belirtti)")
                    else:
                        # Filtreleme başarısız oldu, sadece ilk N tanesini al
                        if len(results) > llm_count:
                            results = results[:llm_count]
                            print(f"⚠️ Filtreleme başarısız, ilk {llm_count} mail seçildi")
                
                except Exception as filter_error:
                    print(f"⚠️ Mail filtreleme hatası: {filter_error}")
                    # Hata durumunda sadece ilk N tanesini al
                    if len(results) > llm_count:
                        results = results[:llm_count]
                        print(f"⚠️ İlk {llm_count} mail seçildi (hata nedeniyle)")
        
    except Exception as e:
        print(f"⚠️ LLM cevabı işlenirken hata: {e}")
        answer = "Arama sonuçlarına göre cevap oluşturulamadı."
    
    return {"answer": answer, "mails": results}

# --- Mail Query Conversations ---
class ConversationMessage(BaseModel):
    type: str  # "user" veya "ai"
    text: str
    timestamp: datetime
    mails: Optional[List[MailSummary]] = None  # AI mesajlarında mail sonuçları

class MailConversation(BaseModel):
    id: Optional[str] = None
    tenant_id: str
    messages: List[ConversationMessage]
    created_at: datetime
    updated_at: datetime

class ConversationCreate(BaseModel):
    messages: List[ConversationMessage]

def get_mail_conversations_collection(db: firestore.Client):
    return db.collection("mail_conversations")

@router.post("/conversations", response_model=MailConversation)
def save_mail_conversation(
    conversation: ConversationCreate,
    current_user: UserInDB = Depends(get_current_user)
):
    """Mail sorgulama konuşmasını kaydeder."""
    firestore_db = firestore.Client()
    conv_col = get_mail_conversations_collection(firestore_db)
    
    now = datetime.now()
    # Messages'ı serialize et (mails bilgisi de dahil)
    messages_data = []
    for msg in conversation.messages:
        msg_dict = msg.model_dump()
        # Mails varsa, MailSummary objelerini dict'e çevir
        if msg_dict.get("mails"):
            msg_dict["mails"] = [mail.model_dump() if hasattr(mail, 'model_dump') else mail for mail in msg_dict["mails"]]
        messages_data.append(msg_dict)
    
    conv_data = {
        "tenant_id": current_user.tenant_id,
        "messages": messages_data,
        "created_at": now,
        "updated_at": now
    }
    
    doc_ref = conv_col.document()
    conv_data["id"] = doc_ref.id
    doc_ref.set(conv_data)
    
    return MailConversation(**conv_data)

@router.get("/conversations", response_model=List[MailConversation])
def get_mail_conversations(
    limit: int = 50,
    current_user: UserInDB = Depends(get_current_user)
):
    """Mail sorgulama konuşmalarını getirir (tarih bazlı, en yeni önce)."""
    firestore_db = firestore.Client()
    conv_col = get_mail_conversations_collection(firestore_db)
    
    query = (
        conv_col.where(filter=FieldFilter("tenant_id", "==", current_user.tenant_id))
        .order_by("updated_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    
    conversations = []
    for doc in query.stream():
        data = doc.to_dict()
        data["id"] = doc.id
        # Timestamp'leri datetime'a çevir
        if "created_at" in data:
            created_at = data["created_at"]
            if hasattr(created_at, 'timestamp'):
                data["created_at"] = datetime.fromtimestamp(created_at.timestamp())
            elif isinstance(created_at, str):
                try:
                    data["created_at"] = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                except:
                    data["created_at"] = datetime.now()
        if "updated_at" in data:
            updated_at = data["updated_at"]
            if hasattr(updated_at, 'timestamp'):
                data["updated_at"] = datetime.fromtimestamp(updated_at.timestamp())
            elif isinstance(updated_at, str):
                try:
                    data["updated_at"] = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
                except:
                    data["updated_at"] = datetime.now()
        # Messages içindeki timestamp'leri ve mails bilgilerini de çevir
        if "messages" in data:
            for msg in data["messages"]:
                if "timestamp" in msg:
                    ts = msg["timestamp"]
                    if hasattr(ts, 'timestamp'):
                        msg["timestamp"] = datetime.fromtimestamp(ts.timestamp())
                    elif isinstance(ts, str):
                        try:
                            msg["timestamp"] = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                        except:
                            msg["timestamp"] = datetime.now()
                # Mails bilgisini parse et (eğer varsa)
                if "mails" in msg and msg["mails"]:
                    parsed_mails = []
                    for mail_data in msg["mails"]:
                        if isinstance(mail_data, dict):
                            # received_at timestamp'ini çevir
                            if "received_at" in mail_data:
                                received_at = mail_data["received_at"]
                                if hasattr(received_at, 'timestamp'):
                                    mail_data["received_at"] = datetime.fromtimestamp(received_at.timestamp())
                                elif isinstance(received_at, str):
                                    try:
                                        mail_data["received_at"] = datetime.fromisoformat(received_at.replace('Z', '+00:00'))
                                    except:
                                        pass
                            parsed_mails.append(MailSummary(**mail_data))
                        else:
                            parsed_mails.append(mail_data)
                    msg["mails"] = parsed_mails
        conversations.append(MailConversation(**data))
    
    return conversations

@router.delete("/conversations/{conversation_id}")
def delete_mail_conversation(
    conversation_id: str,
    current_user: UserInDB = Depends(get_current_user)
):
    """Mail sorgulama konuşmasını siler."""
    firestore_db = firestore.Client()
    conv_col = get_mail_conversations_collection(firestore_db)
    
    doc = conv_col.document(conversation_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Konuşma bulunamadı.")
    
    data = doc.to_dict()
    if data.get("tenant_id") != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Bu konuşmaya erişim yetkiniz yok.")
    
    conv_col.document(conversation_id).delete()
    return {"message": "Konuşma silindi."}

class TaskWithMailId(BaseModel):
    task: str
    mail_id: str
    received_at: Optional[datetime] = None  # Mail tarihi
    subject: str  # Mail konusu

@router.get("/tasks", response_model=List[TaskWithMailId])
def get_potential_tasks(
    period: str = "daily",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """Potansiyel görev atamalarını getirir."""
    firestore_db = firestore.Client()
    mail_col = get_mail_collection(firestore_db)
    
    query = mail_col.where(filter=FieldFilter("tenant_id", "==", current_user.tenant_id))
    
    # UTC+3 (Türkiye saati) kullanarak tarih filtreleme
    if period == "daily":
        start = get_today_start_utc3()
        print(f"📅 Potansiyel görevler - Günlük özet - Bugünün başlangıcı (UTC+3'e göre): {start}")
        query = query.where(filter=FieldFilter("received_at", ">=", start))
    elif period == "weekly":
        now_utc3 = get_now_utc3()
        start = now_utc3 - timedelta(days=7)
        print(f"📅 Potansiyel görevler - Haftalık özet - 7 gün öncesi (UTC+3'e göre): {start}")
        query = query.where(filter=FieldFilter("received_at", ">=", start))
    elif period == "custom" and start_date and end_date:
        try:
            if isinstance(start_date, str):
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            else:
                start_dt = start_date
            if isinstance(end_date, str):
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                end_dt = end_dt.replace(hour=23, minute=59, second=59)
            else:
                end_dt = end_date
            query = query.where(filter=FieldFilter("received_at", ">=", start_dt))
            query = query.where(filter=FieldFilter("received_at", "<=", end_dt))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Geçersiz tarih formatı: {str(e)}")
    
    all_tasks = []
    all_mails = []
    
    # Tüm mailleri topla ve thread'lere göre grupla
    for doc in query.stream():
        data = doc.to_dict()
        # Tarih kontrolü - Firestore timestamp'ini datetime'a çevir
        received_at = data.get("received_at")
        if received_at:
            if hasattr(received_at, 'timestamp'):
                received_at = datetime.fromtimestamp(received_at.timestamp())
            elif not isinstance(received_at, datetime):
                try:
                    if isinstance(received_at, str):
                        received_at = datetime.fromisoformat(received_at.replace('Z', '+00:00'))
                    else:
                        received_at = datetime.now()
                except:
                    received_at = datetime.now()
            
            # Custom period için tarih kontrolü (ekstra güvenlik)
            if period == "custom" and start_date and end_date:
                try:
                    if isinstance(start_date, str):
                        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                    else:
                        start_dt = start_date
                    if isinstance(end_date, str):
                        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                        end_dt = end_dt.replace(hour=23, minute=59, second=59)
                    else:
                        end_dt = end_date
                    
                    if received_at < start_dt or received_at > end_dt:
                        continue
                except Exception as e:
                    print(f"⚠️ Tarih kontrolü hatası: {e}")
        
        data["id"] = doc.id
        all_mails.append(data)
    
    # Thread'lere göre grupla
    threads = {}  # {thread_id: [mail1, mail2, ...]}
    for mail in all_mails:
        thread_id = mail.get("thread_id") or mail.get("message_id") or mail.get("id")
        if thread_id not in threads:
            threads[thread_id] = []
        threads[thread_id].append(mail)
    
    # Her thread için potansiyel görevleri topla
    for thread_id, thread_mails in threads.items():
        if len(thread_mails) == 1:
            # Tek mail ise direkt görevleri ekle
            mail = thread_mails[0]
            tasks = mail.get("potential_tasks", [])
            mail_id = mail.get("id")
            mail_subject = mail.get("subject", "Konu bilinmiyor")
            # Mail tarihini al
            mail_received_at = mail.get("received_at")
            if mail_received_at:
                if hasattr(mail_received_at, 'timestamp'):
                    mail_received_at = datetime.fromtimestamp(mail_received_at.timestamp())
                elif isinstance(mail_received_at, str):
                    try:
                        mail_received_at = datetime.fromisoformat(mail_received_at.replace('Z', '+00:00'))
                    except:
                        mail_received_at = None
            for task in tasks:
                all_tasks.append(TaskWithMailId(task=task, mail_id=mail_id, received_at=mail_received_at, subject=mail_subject))
        else:
            # Birden fazla mail varsa - thread özetindeki görevleri VE thread içindeki tüm maillerin görevlerini birleştir
            # Thread özetindeki görevler (eğer thread_id bir mail ID'si ise)
            thread_summary_tasks = []
            thread_mail_id = None
            thread_received_at = None
            
            # Thread ID'si bir mail ID'si mi kontrol et
            for mail in thread_mails:
                if mail.get("id") == thread_id:
                    thread_summary_tasks = mail.get("potential_tasks", [])
                    thread_mail_id = mail.get("id")
                    # Thread özet mailinin tarihini al
                    thread_received_at = mail.get("received_at")
                    if thread_received_at:
                        if hasattr(thread_received_at, 'timestamp'):
                            thread_received_at = datetime.fromtimestamp(thread_received_at.timestamp())
                        elif isinstance(thread_received_at, str):
                            try:
                                thread_received_at = datetime.fromisoformat(thread_received_at.replace('Z', '+00:00'))
                            except:
                                thread_received_at = None
                    break
            
            # Thread özetindeki görevleri ekle (eğer varsa)
            if thread_mail_id:
                thread_subject = None
                for m in thread_mails:
                    if m.get("id") == thread_mail_id:
                        thread_subject = m.get("subject", "Konu bilinmiyor")
                        break
                if not thread_subject:
                    thread_subject = thread_mails[0].get("subject", "Konu bilinmiyor") if thread_mails else "Konu bilinmiyor"
                for task in thread_summary_tasks:
                    all_tasks.append(TaskWithMailId(task=task, mail_id=thread_mail_id, received_at=thread_received_at, subject=thread_subject))
            
            # Thread içindeki TÜM maillerin görevlerini de ekle
            for mail in thread_mails:
                mail_id = mail.get("id")
                mail_subject = mail.get("subject", "Konu bilinmiyor")
                tasks = mail.get("potential_tasks", [])
                # Mail tarihini al
                mail_received_at = mail.get("received_at")
                if mail_received_at:
                    if hasattr(mail_received_at, 'timestamp'):
                        mail_received_at = datetime.fromtimestamp(mail_received_at.timestamp())
                    elif isinstance(mail_received_at, str):
                        try:
                            mail_received_at = datetime.fromisoformat(mail_received_at.replace('Z', '+00:00'))
                        except:
                            mail_received_at = None
                for task in tasks:
                    # Duplicate kontrolü - aynı görev zaten eklenmişse atla
                    if not any(t.task == task and t.mail_id == mail_id for t in all_tasks):
                        all_tasks.append(TaskWithMailId(task=task, mail_id=mail_id, received_at=mail_received_at, subject=mail_subject))
    
    # Tarihe göre sırala (yeniden eskiye - en yeni önce)
    all_tasks.sort(key=lambda x: x.received_at if x.received_at else datetime.min, reverse=True)
    
    # Limit'i kaldırdık - tüm görevleri döndür
    print(f"📊 Toplam {len(all_tasks)} potansiyel görev bulundu (period: {period})")
    return all_tasks

class DateWithMailId(BaseModel):
    date: str
    mail_id: str
    subject: str

@router.get("/critical-dates", response_model=Dict[str, List[DateWithMailId]])
def get_critical_dates(
    period: str = "daily",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """Kritik tarihleri getirir."""
    firestore_db = firestore.Client()
    mail_col = get_mail_collection(firestore_db)
    
    query = mail_col.where(filter=FieldFilter("tenant_id", "==", current_user.tenant_id))
    
    # UTC+3 (Türkiye saati) kullanarak tarih filtreleme
    if period == "daily":
        start = get_today_start_utc3()
        print(f"📅 Kritik tarihler - Günlük özet - Bugünün başlangıcı (UTC+3'e göre): {start}")
        query = query.where(filter=FieldFilter("received_at", ">=", start))
    elif period == "weekly":
        now_utc3 = get_now_utc3()
        start = now_utc3 - timedelta(days=7)
        print(f"📅 Kritik tarihler - Haftalık özet - 7 gün öncesi (UTC+3'e göre): {start}")
        query = query.where(filter=FieldFilter("received_at", ">=", start))
    elif period == "custom" and start_date and end_date:
        try:
            if isinstance(start_date, str):
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            else:
                start_dt = start_date
            if isinstance(end_date, str):
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                end_dt = end_dt.replace(hour=23, minute=59, second=59)
            else:
                end_dt = end_date
            query = query.where(filter=FieldFilter("received_at", ">=", start_dt))
            query = query.where(filter=FieldFilter("received_at", "<=", end_dt))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Geçersiz tarih formatı: {str(e)}")
    
    dates = {"contract_renewal": [], "delivery": [], "meeting": [], "deadline": []}
    for doc in query.stream():
        data = doc.to_dict()
        critical_dates = data.get("critical_dates", {})
        mail_id = doc.id
        mail_subject = data.get("subject", "Konu bilinmiyor")
        for key in dates.keys():
            date_value = critical_dates.get(key)
            if date_value:
                # date_value string, list veya dict olabilir
                if isinstance(date_value, str):
                    # String ise direkt ekle
                    dates[key].append(DateWithMailId(date=date_value, mail_id=mail_id, subject=mail_subject))
                elif isinstance(date_value, list):
                    # List ise her birini ekle
                    for date_item in date_value:
                        if isinstance(date_item, str):
                            dates[key].append(DateWithMailId(date=date_item, mail_id=mail_id, subject=mail_subject))
                elif isinstance(date_value, dict):
                    # Dict ise içindeki tüm değerleri düzleştir
                    for sub_key, sub_value in date_value.items():
                        if isinstance(sub_value, str):
                            dates[key].append(DateWithMailId(date=sub_value, mail_id=mail_id, subject=mail_subject))
                        elif isinstance(sub_value, list):
                            for date_item in sub_value:
                                if isinstance(date_item, str):
                                    dates[key].append(DateWithMailId(date=date_item, mail_id=mail_id, subject=mail_subject))
                else:
                    # Diğer tipler için string'e çevir
                    dates[key].append(DateWithMailId(date=str(date_value), mail_id=mail_id, subject=mail_subject))
    
    return dates

# --- ÖNEMLİ HATIRLATMALAR ÖZELLİĞİ ---

class UrgentReminder(BaseModel):
    type: str  # "critical_mail", "meeting", "deadline", "delivery", "contract_renewal"
    title: str
    description: str
    date: Optional[str] = None  # Tarih string formatında
    time: Optional[str] = None  # Saat bilgisi (varsa)
    mail_id: str
    priority: str = "high"  # "high", "medium", "low"

class UrgentRemindersResponse(BaseModel):
    today: List[UrgentReminder]
    tomorrow: List[UrgentReminder]
    this_week: List[UrgentReminder]
    summary: Optional[str] = None  # Bugün/yarın/bu hafta için özet metin

class ReminderPreferences(BaseModel):
    show_critical_mails: bool = True
    show_meetings: bool = True
    show_deadlines: bool = True
    show_deliveries: bool = True
    show_contract_renewals: bool = True
    show_today: bool = True
    show_tomorrow: bool = True
    show_this_week: bool = True

def parse_date_string(date_str: str) -> Optional[datetime]:
    """Tarih string'ini datetime'a çevirir. Çeşitli formatları destekler."""
    if not date_str:
        return None
    
    # Yaygın tarih formatları
    date_formats = [
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d/%m/%Y %H:%M",
    ]
    
    for fmt in date_formats:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except:
            continue
    
    # Tarih parse edilemezse None döndür
    return None

def is_date_today_or_tomorrow(date_str: str) -> tuple[bool, bool]:
    """Tarih bugün veya yarın mı kontrol eder. (is_today, is_tomorrow) döndürür. UTC+3'e göre."""
    if not date_str:
        return False, False
    
    parsed_date = parse_date_string(date_str)
    if not parsed_date:
        return False, False
    
    # UTC+3'e göre bugün ve yarın
    now_utc3 = datetime.now(TURKEY_TIMEZONE)
    today = now_utc3.date()
    tomorrow = today + timedelta(days=1)
    
    # Tarihi UTC+3'e göre kontrol et
    if parsed_date.tzinfo is None:
        # Timezone yoksa UTC+3 olarak kabul et
        date_only = parsed_date.date()
    else:
        # UTC+3'e çevir
        date_only = parsed_date.astimezone(TURKEY_TIMEZONE).date()
    
    is_today = date_only == today
    is_tomorrow = date_only == tomorrow
    
    return is_today, is_tomorrow

@router.get("/urgent-reminders", response_model=UrgentRemindersResponse)
def get_urgent_reminders(
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """Bugün ve yarın için kritik mailleri ve önemli tarihleri getirir."""
    firestore_db = firestore.Client()
    mail_col = get_mail_collection(firestore_db)
    
    # Kullanıcı tercihlerini al
    user_prefs_doc = firestore_db.collection("user_reminder_preferences").document(current_user.id).get()
    prefs = ReminderPreferences()
    if user_prefs_doc.exists:
        prefs_data = user_prefs_doc.to_dict()
        prefs = ReminderPreferences(**prefs_data)
    
    # UTC+3'e göre bugün, yarın ve bu hafta tarihlerini hesapla
    now_utc3 = datetime.now(TURKEY_TIMEZONE)
    today = now_utc3.date()
    tomorrow = today + timedelta(days=1)
    
    # Bu haftanın başlangıcı (Pazartesi) ve bitişi (Pazar)
    days_since_monday = now_utc3.weekday()  # 0 = Pazartesi, 6 = Pazar
    week_start = today - timedelta(days=days_since_monday)
    week_end = week_start + timedelta(days=6)
    
    today_start_utc3 = datetime(today.year, today.month, today.day, tzinfo=TURKEY_TIMEZONE)
    tomorrow_end_utc3 = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 23, 59, 59, tzinfo=TURKEY_TIMEZONE)
    week_start_utc3 = datetime(week_start.year, week_start.month, week_start.day, tzinfo=TURKEY_TIMEZONE)
    week_end_utc3 = datetime(week_end.year, week_end.month, week_end.day, 23, 59, 59, tzinfo=TURKEY_TIMEZONE)
    
    # UTC'ye çevir (Firestore UTC kullanır)
    today_start = today_start_utc3.astimezone(timezone.utc).replace(tzinfo=None)
    tomorrow_end = tomorrow_end_utc3.astimezone(timezone.utc).replace(tzinfo=None)
    week_start_utc = week_start_utc3.astimezone(timezone.utc).replace(tzinfo=None)
    week_end_utc = week_end_utc3.astimezone(timezone.utc).replace(tzinfo=None)
    
    # Son 7 günün maillerini al (bugün/yarın/bu hafta için kritik olanları bulmak için)
    start_date = today_start - timedelta(days=7)
    query = (
        mail_col.where(filter=FieldFilter("tenant_id", "==", current_user.tenant_id))
        .where(filter=FieldFilter("received_at", ">=", start_date))
        .where(filter=FieldFilter("received_at", "<=", week_end_utc))
    )
    
    today_reminders = []
    tomorrow_reminders = []
    this_week_reminders = []
    
    for doc in query.stream():
        data = doc.to_dict()
        mail_id = doc.id
        is_critical = data.get("is_critical", False)
        critical_dates = data.get("critical_dates", {})
        subject = data.get("subject", "")
        sender = data.get("sender", "")
        summary = data.get("summary", "")
        
        # Kritik mailler (bugün/yarın alınan)
        if prefs.show_critical_mails and is_critical:
            received_at = data.get("received_at")
            if received_at:
                if hasattr(received_at, 'timestamp'):
                    received_at = datetime.fromtimestamp(received_at.timestamp())
                elif isinstance(received_at, str):
                    try:
                        received_at = datetime.fromisoformat(received_at.replace('Z', '+00:00'))
                    except:
                        received_at = None
                
                if received_at:
                    # UTC+3'e çevir
                    if received_at.tzinfo is None:
                        received_at_utc3 = received_at.replace(tzinfo=timezone.utc).astimezone(TURKEY_TIMEZONE)
                    else:
                        received_at_utc3 = received_at.astimezone(TURKEY_TIMEZONE)
                    received_date = received_at_utc3.date()
                    
                    if received_date == today:
                        today_reminders.append(UrgentReminder(
                            type="critical_mail",
                            title=f"Kritik Mail: {subject}",
                            description=f"{sender}: {summary[:100] if summary else subject}",
                            mail_id=mail_id,
                            priority="high"
                        ))
                    elif received_date == tomorrow:
                        tomorrow_reminders.append(UrgentReminder(
                            type="critical_mail",
                            title=f"Kritik Mail: {subject}",
                            description=f"{sender}: {summary[:100] if summary else subject}",
                            mail_id=mail_id,
                            priority="high"
                        ))
        
        # Kritik tarihleri kontrol et
        for date_type, date_value in critical_dates.items():
            if not date_value:
                continue
            
            # Kullanıcı tercihlerine göre filtrele
            if date_type == "meeting" and not prefs.show_meetings:
                continue
            if date_type == "deadline" and not prefs.show_deadlines:
                continue
            if date_type == "delivery" and not prefs.show_deliveries:
                continue
            if date_type == "contract_renewal" and not prefs.show_contract_renewals:
                continue
            
            # Tarih değerini işle (string, list veya dict olabilir)
            date_strings = []
            if isinstance(date_value, str):
                date_strings.append(date_value)
            elif isinstance(date_value, list):
                date_strings.extend([str(d) for d in date_value if d])
            elif isinstance(date_value, dict):
                for v in date_value.values():
                    if isinstance(v, str):
                        date_strings.append(v)
                    elif isinstance(v, list):
                        date_strings.extend([str(d) for d in v if d])
            
            # Her tarih için bugün/yarın/bu hafta kontrolü yap
            for date_str in date_strings:
                is_today, is_tomorrow = is_date_today_or_tomorrow(date_str)
                
                # Bu hafta kontrolü
                is_this_week = False
                parsed_date = parse_date_string(date_str)
                if parsed_date:
                    if parsed_date.tzinfo is None:
                        parsed_date_utc3 = parsed_date.replace(tzinfo=TURKEY_TIMEZONE)
                    else:
                        parsed_date_utc3 = parsed_date.astimezone(TURKEY_TIMEZONE)
                    date_only = parsed_date_utc3.date()
                    # Bugün ve yarın hariç, bu hafta içinde mi?
                    if not is_today and not is_tomorrow and week_start <= date_only <= week_end:
                        is_this_week = True
                
                if (is_today and prefs.show_today) or (is_tomorrow and prefs.show_tomorrow) or (is_this_week and prefs.show_this_week):
                    # Tarih tipine göre başlık ve açıklama oluştur
                    type_names = {
                        "meeting": "Toplantı",
                        "deadline": "Son Tarih",
                        "delivery": "Teslim Tarihi",
                        "contract_renewal": "Sözleşme Yenileme"
                    }
                    
                    type_name = type_names.get(date_type, date_type)
                    time_str = None
                    if parsed_date:
                        if parsed_date.tzinfo is None:
                            parsed_date_utc3 = parsed_date.replace(tzinfo=TURKEY_TIMEZONE)
                        else:
                            parsed_date_utc3 = parsed_date.astimezone(TURKEY_TIMEZONE)
                        if parsed_date_utc3.hour != 0 or parsed_date_utc3.minute != 0:
                            time_str = parsed_date_utc3.strftime("%H:%M")
                    
                    reminder = UrgentReminder(
                        type=date_type,
                        title=f"{type_name}: {subject}",
                        description=f"{sender}: {summary[:100] if summary else subject}",
                        date=date_str,
                        time=time_str,
                        mail_id=mail_id,
                        priority="high" if date_type in ["deadline", "meeting"] else "medium"
                    )
                    
                    if is_today:
                        today_reminders.append(reminder)
                    elif is_tomorrow:
                        tomorrow_reminders.append(reminder)
                    elif is_this_week:
                        this_week_reminders.append(reminder)
    
    # Özet metin oluştur
    summary_parts = []
    if today_reminders:
        critical_count = len([r for r in today_reminders if r.type == "critical_mail"])
        meeting_count = len([r for r in today_reminders if r.type == "meeting"])
        deadline_count = len([r for r in today_reminders if r.type == "deadline"])
        
        today_summary = []
        if critical_count > 0:
            today_summary.append(f"{critical_count} kritik mail")
        if meeting_count > 0:
            today_summary.append(f"{meeting_count} toplantı")
        if deadline_count > 0:
            today_summary.append(f"{deadline_count} son tarih")
        
        if today_summary:
            summary_parts.append(f"Bugün: {', '.join(today_summary)}")
    
    if tomorrow_reminders:
        critical_count = len([r for r in tomorrow_reminders if r.type == "critical_mail"])
        meeting_count = len([r for r in tomorrow_reminders if r.type == "meeting"])
        deadline_count = len([r for r in tomorrow_reminders if r.type == "deadline"])
        
        tomorrow_summary = []
        if critical_count > 0:
            tomorrow_summary.append(f"{critical_count} kritik mail")
        if meeting_count > 0:
            tomorrow_summary.append(f"{meeting_count} toplantı")
        if deadline_count > 0:
            tomorrow_summary.append(f"{deadline_count} son tarih")
        
        if tomorrow_summary:
            summary_parts.append(f"Yarın: {', '.join(tomorrow_summary)}")
    
    if this_week_reminders:
        meeting_count = len([r for r in this_week_reminders if r.type == "meeting"])
        deadline_count = len([r for r in this_week_reminders if r.type == "deadline"])
        
        week_summary = []
        if meeting_count > 0:
            week_summary.append(f"{meeting_count} toplantı")
        if deadline_count > 0:
            week_summary.append(f"{deadline_count} son tarih")
        
        if week_summary:
            summary_parts.append(f"Bu hafta: {', '.join(week_summary)}")
    
    summary_text = " | ".join(summary_parts) if summary_parts else None
    
    # Önceliğe göre sırala (high -> medium -> low)
    priority_order = {"high": 0, "medium": 1, "low": 2}
    today_reminders.sort(key=lambda x: (priority_order.get(x.priority, 2), x.type))
    tomorrow_reminders.sort(key=lambda x: (priority_order.get(x.priority, 2), x.type))
    this_week_reminders.sort(key=lambda x: (priority_order.get(x.priority, 2), x.type))
    
    return UrgentRemindersResponse(
        today=today_reminders,
        tomorrow=tomorrow_reminders,
        this_week=this_week_reminders,
        summary=summary_text
    )

@router.get("/reminder-preferences", response_model=ReminderPreferences)
def get_reminder_preferences(
    current_user: UserInDB = Depends(get_current_user)
):
    """Kullanıcının hatırlatma tercihlerini getirir."""
    firestore_db = firestore.Client()
    user_prefs_doc = firestore_db.collection("user_reminder_preferences").document(current_user.id).get()
    
    if user_prefs_doc.exists:
        prefs_data = user_prefs_doc.to_dict()
        return ReminderPreferences(**prefs_data)
    else:
        # Varsayılan tercihler
        return ReminderPreferences()

@router.put("/reminder-preferences", response_model=ReminderPreferences)
def update_reminder_preferences(
    preferences: ReminderPreferences,
    current_user: UserInDB = Depends(get_current_user)
):
    """Kullanıcının hatırlatma tercihlerini günceller."""
    firestore_db = firestore.Client()
    prefs_dict = preferences.model_dump()
    
    firestore_db.collection("user_reminder_preferences").document(current_user.id).set(prefs_dict, merge=True)
    
    return preferences

@router.get("/{mail_id}", response_model=MailSummary)
def get_mail_by_id(
    mail_id: str,
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository),
    storage: BaseStorageAdapter = Depends(get_storage_adapter)
):
    """Mail ID'ye göre mail detayını getirir. Ek özetleri mail çekilirken oluşturulur, burada sadece mevcut veriyi döndürür."""
    firestore_db = firestore.Client()
    mail_col = get_mail_collection(firestore_db)
    doc = mail_col.document(mail_id).get()
    
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Mail bulunamadı.")
    
    data = doc.to_dict()
    if data.get("tenant_id") != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Bu mail'e erişim yetkiniz yok.")
    
    # received_at timestamp'ini datetime'a çevir
    if "received_at" in data:
        received_at = data["received_at"]
        if hasattr(received_at, 'timestamp'):
            data["received_at"] = datetime.fromtimestamp(received_at.timestamp())
        elif isinstance(received_at, str):
            try:
                data["received_at"] = datetime.fromisoformat(received_at.replace('Z', '+00:00'))
            except:
                pass
    
    # Mail detayı döndürülmeden önce, maili okunmuş olarak işaretle
    if not data.get("is_read", False):
        doc_ref = mail_col.document(mail_id)
        doc_ref.update({"is_read": True})
        data["is_read"] = True
        print(f"✅ Mail {mail_id} okunmuş olarak işaretlendi")
    
    data["id"] = doc.id
    return MailSummary(**data)