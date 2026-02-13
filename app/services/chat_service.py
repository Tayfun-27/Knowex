# backend/app/services/chat_service.py
# Ana chat mesaj işleme servisi

import re
import traceback
import io
from typing import List, Dict, Any, Set
import time
from fastapi import HTTPException, status 

from app.schemas.chat import ChatRequest, ChatResponse, ChatMessage, ActiveContextFile
from app.schemas.user import UserInDB
from app.schemas.file import FileOut 
from app.repositories.base import BaseRepository
from app.storage_adapters.base import BaseStorageAdapter
from app.services import vector_service

# --- LangChain Importları ---
from langchain_core.documents import Document
from langchain_core.runnables import RunnableLambda
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser 
# --- Bitti ---

# --- Modüler Importlar ---
from app.services.llm_providers import get_llm_for_model, get_cheap_llm
from app.services.token_tracking import TokenTracker, extract_token_usage_from_response
from app.services.chat_helpers import (
    normalize_text_for_matching,
    calculate_filename_match_score,
    identify_and_filter_high_confidence_document,
    is_list_intent,
    rerank_chunks_with_llm_wrapper,
    rerank_chunks_with_cross_encoder,
    create_hypothetical_document_for_query_wrapper,
    is_off_topic_query,
    is_help_or_support_query,
    get_help_response,
    is_greeting_query,
    get_greeting_response
)
from app.services.prompts import RAG_PROMPT_TEMPLATE


# --- Context Memory ---

class ContextMemory:
    # ... (Bu sınıf değişmedi) ...
    def __init__(self): self.context_items: Dict[str, ActiveContextFile] = {}
    def set_context(self, items: List[ActiveContextFile]):
        self.context_items = {item.id: item for item in items}
        print(f"🧠 Bağlam Hafzası Ayarlandı: {len(self.context_items)} kalem.")
    def get_context(self) -> List[ActiveContextFile]: return list(self.context_items.values())
    def get_file_ids(self) -> Set[str]: return {item.id for item in self.context_items.values() if item.type == 'file'}
    def get_folder_ids(self) -> Set[str]: return {item.id for item in self.context_items.values() if item.type == 'folder'}
    def has_context(self) -> bool: return bool(self.context_items)
    def clear(self):
        self.context_items = {}
        print("🗑️ Bağlam Hafzası Temizlendi")

_context_memory_store: Dict[str, ContextMemory] = {}

def get_context_memory_for_chat(chat_id: str) -> ContextMemory:
    if chat_id not in _context_memory_store: _context_memory_store[chat_id] = ContextMemory()
    return _context_memory_store[chat_id]

# --- Güvenlik Fonksiyonu (Değişiklik yok) ---
def get_all_accessible_files_for_user(db: BaseRepository, user: UserInDB) -> List[FileOut]:
    if user.role == "Admin":
        print(f"Kullanıcı '{user.email}' Admin. Tüm tenant dosyaları getiriliyor.")
        return db.get_all_files_for_tenant(tenant_id=user.tenant_id)

    print(f"Kullanıcı '{user.email}' (Rol: {user.role}) için erişilebilir dosyalar hesaplanıyor...")
    user_role = db.get_role_by_name(tenant_id=user.tenant_id, role_name=user.role)
    allowed_folder_ids = set()
    allowed_file_ids = set()
    if user_role:
        allowed_folder_ids = set(user_role.allowed_folders or [])
        allowed_file_ids = set(user_role.allowed_files or [])
    all_tenant_files = db.get_all_files_for_tenant(tenant_id=user.tenant_id)
    accessible_files: List[FileOut] = []
    for file in all_tenant_files:
        is_owner = file.owner_id == user.id
        is_file_allowed = file.id in allowed_file_ids
        is_folder_allowed = file.folder_id and file.folder_id in allowed_folder_ids
        if is_owner or is_file_allowed or is_folder_allowed:
            accessible_files.append(file)
    print(f"Kullanıcı {len(accessible_files)} adet dosyaya erişebilir.")
    return accessible_files

# --- Yardımcı fonksiyonlar chat_helpers.py'de ---

def is_simple_query(query: str) -> bool:
    """Sorgunun basit (tek adımlı/olgusal) olup olmadığını tahmin et."""
    query_lower = query.lower()
    
    # 1. Kısa sorgular genellikle basittir
    if len(query.split()) < 5:
        return True
        
    # 2. Selamlaşma ve basit etkileşimler
    greetings = ['merhaba', 'selam', 'günaydın', 'iyi günler', 'nasılsın', 'kimsin', 'ne yapabilirsin']
    if any(g in query_lower for g in greetings):
        return True
        
    # 3. Basit "nedir", "ne zaman" soruları (eğer çok karmaşık değilse)
    simple_starters = ['nedir', 'ne zaman', 'kim', 'nerede', 'kaç']
    # Eğer "karşılaştır", "analiz et", "özetle", "farkı nedir" gibi karmaşık ifadeler yoksa
    complex_indicators = ['karşılaştır', 'analiz', 'özetle', 'fark', 'ilişki', 'neden', 'nasıl', 'yorumla', 'değerlendir']
    
    if any(s in query_lower for s in simple_starters) and not any(c in query_lower for c in complex_indicators):
        return True
        
    return False

def _get_file_bytes(file_record: FileOut, user: UserInDB, storage: BaseStorageAdapter) -> bytes:
    """
    Dosya içeriğini döndürür. External storage dosyaları için Google Drive/OneDrive'dan indirir.
    """
    # Eğer dosya external storage'dan geliyorsa, Google Drive/OneDrive'dan indir
    if file_record.external_file_id and file_record.external_storage_type:
        from google.cloud import firestore
        from app.storage_adapters.google_drive_adapter import GoogleDriveAdapter
        from app.storage_adapters.onedrive_adapter import OneDriveAdapter
        from app.core.config import GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET, ONEDRIVE_CLIENT_ID, ONEDRIVE_CLIENT_SECRET
        
        firestore_db = firestore.Client()
        storage_type = file_record.external_storage_type
        
        # Kullanıcının storage bağlantısını al
        if storage_type == "google_drive":
            user_storage = firestore_db.collection("user_external_storage").document(user.id).get()
            if not user_storage.exists:
                # Admin seviyesinde bağlantıyı kontrol et
                admin_settings = firestore_db.collection("external_storage_settings").document(user.tenant_id).get()
                if not admin_settings.exists:
                    raise Exception("Google Drive bağlantısı bulunamadı")
                admin_data = admin_settings.to_dict()
                access_token = admin_data.get('google_drive_access_token')
                refresh_token = admin_data.get('google_drive_refresh_token')
                client_id = GOOGLE_DRIVE_CLIENT_ID
                client_secret = GOOGLE_DRIVE_CLIENT_SECRET
            else:
                storage_data = user_storage.to_dict()
                access_token = storage_data.get('access_token')
                refresh_token = storage_data.get('refresh_token')
                client_id = GOOGLE_DRIVE_CLIENT_ID
                client_secret = GOOGLE_DRIVE_CLIENT_SECRET
            
            adapter = GoogleDriveAdapter()
        elif storage_type == "onedrive":
            user_storage = firestore_db.collection("user_external_storage").document(user.id).get()
            if not user_storage.exists:
                # Admin seviyesinde bağlantıyı kontrol et
                admin_settings = firestore_db.collection("external_storage_settings").document(user.tenant_id).get()
                if not admin_settings.exists:
                    raise Exception("OneDrive bağlantısı bulunamadı")
                admin_data = admin_settings.to_dict()
                access_token = admin_data.get('onedrive_access_token')
                refresh_token = admin_data.get('onedrive_refresh_token')
                client_id = ONEDRIVE_CLIENT_ID
                client_secret = ONEDRIVE_CLIENT_SECRET
            else:
                storage_data = user_storage.to_dict()
                access_token = storage_data.get('access_token')
                refresh_token = storage_data.get('refresh_token')
                client_id = ONEDRIVE_CLIENT_ID
                client_secret = ONEDRIVE_CLIENT_SECRET
            
            adapter = OneDriveAdapter()
        else:
            raise Exception(f"Desteklenmeyen storage tipi: {storage_type}")
        
        if not access_token:
            raise Exception(f"{storage_type} bağlantısı bulunamadı")
        
        # Token'ı kontrol et ve gerekirse yenile
        try:
            if storage_type == "google_drive":
                file_bytes = adapter.download_file(
                    file_id=file_record.external_file_id,
                    access_token=access_token,
                    mime_type=file_record.content_type
                )
            else:  # onedrive
                file_bytes = adapter.download_file(
                    file_id=file_record.external_file_id,
                    access_token=access_token
                )
        except Exception as e:
            # Token süresi dolmuş olabilir
            if refresh_token and client_id and client_secret:
                try:
                    tokens = adapter.refresh_access_token(
                        refresh_token=refresh_token,
                        client_id=client_id,
                        client_secret=client_secret
                    )
                    access_token = tokens['access_token']
                    
                    # Token'ı güncelle
                    if user_storage.exists:
                        firestore_db.collection("user_external_storage").document(user.id).update({
                            'access_token': access_token
                        })
                    else:
                        # Admin seviyesinde güncelle
                        update_data = {}
                        if storage_type == "google_drive":
                            update_data['google_drive_access_token'] = access_token
                        else:
                            update_data['onedrive_access_token'] = access_token
                        firestore_db.collection("external_storage_settings").document(user.tenant_id).update(update_data)
                    
                    # Tekrar dene
                    if storage_type == "google_drive":
                        file_bytes = adapter.download_file(
                            file_id=file_record.external_file_id,
                            access_token=access_token,
                            mime_type=file_record.content_type
                        )
                    else:
                        file_bytes = adapter.download_file(
                            file_id=file_record.external_file_id,
                            access_token=access_token
                        )
                except Exception as refresh_error:
                    raise Exception(f"Dosya indirilemedi (token yenileme başarısız): {refresh_error}")
            else:
                raise Exception(f"Dosya indirilemedi: {e}")
        
        return file_bytes
    else:
        # Normal dosyalar için mevcut mantık
        if not file_record.storage_path:
            raise Exception("Dosya storage path'i bulunamadı.")
        return storage.download_file_content(storage_path=file_record.storage_path)

# --- ANA FONKSİYON ---
def process_chat_message(
    request: ChatRequest, user: UserInDB, db: BaseRepository, storage: BaseStorageAdapter
) -> ChatResponse:
    # traceback modülünü fonksiyon başında kullanılabilir hale getir
    # (Python'ın local variable algılamasını önlemek için)
    import traceback as tb_module
    
    t_start = time.monotonic()
    
    # Model seçimini al
    model_name = request.model_name or "gemini"
    agent_type = request.agent_type or "default"
    
    # Eğer agent_type default ise ama önceki mesajlarda özel agent (excel, presentation) kullanılmışsa, devam ettir
    if agent_type == "default" and request.chat_id:
        try:
            print(f"🔍 Agent type kontrolü: chat_id={request.chat_id}, agent_type={agent_type}")
            previous_messages = db.get_chat_messages(chat_id=request.chat_id, tenant_id=user.tenant_id)
            print(f"🔍 Önceki mesaj sayısı: {len(previous_messages)}")
            # Son AI mesajını kontrol et
            for msg in reversed(previous_messages):
                if msg.sender == "ai" and msg.metadata:
                    metadata = msg.metadata
                    print(f"🔍 AI mesaj metadata: {metadata}")
                    previous_agent_type = metadata.get("agent_type")
                    
                    # Presentation agent eksik bilgi toplama aşamasında, devam ettir
                    if previous_agent_type == "presentation" and metadata.get("presentation_state") == "collecting_info":
                        agent_type = "presentation"
                        print(f"✅ Presentation Agent: Önceki sohbette eksik bilgi toplama aşamasında, devam ediliyor")
                        break
                    # Excel agent kullanılmışsa, devam ettir
                    elif previous_agent_type == "excel":
                        agent_type = "excel"
                        print(f"✅ Excel Agent: Önceki sohbette Excel agent kullanılmış, devam ediliyor")
                        break
        except Exception as e:
            print(f"⚠️ Önceki mesaj kontrolü hatası: {e}")
            tb_module.print_exc()
    
    print(f"\n🤖 KULLANILAN MODEL: {model_name}, AGENT TİPİ: {agent_type}\n")
    
    # Excel agent için özel işleme
    if agent_type == "excel":
        try:
            from app.services.excel_agent_service import analyze_excel_data, compare_excel_files
            
            # Context dosyalarını kontrol et
            if request.context_files and len(request.context_files) > 0:
                excel_files = [f for f in request.context_files if f.type == "file"]
                
                if len(excel_files) == 1:
                    # Tek Excel dosyası analizi
                    file_record = db.get_file_by_id(user.tenant_id, excel_files[0].id)
                    if file_record:
                        # CSV desteği için uzantı kontrolünü genişlettik
                        file_name_lower = file_record.name.lower()
                        if file_name_lower.endswith(('.xlsx', '.xls', '.csv')):
                            print(f"📊 Excel Agent: Tek dosya analizi başlatılıyor - '{file_record.name}'")
                            file_bytes = _get_file_bytes(file_record, user, storage)
                            
                            # GÜNCELLEME BURADA: file_name parametresi eklendi
                            response_text = analyze_excel_data(
                                file_bytes=file_bytes,
                                question=request.message,
                                model_name=model_name,
                                file_name=file_record.name  # <--- Dosya adı eklendi
                            )
                            
                            chat_id = request.chat_id or db.create_chat_session(
                                user.id, user.tenant_id, request.message[:40] + "..."
                            ).id
                            db.save_chat_message(chat_id, user.tenant_id, ChatMessage(sender="user", text=request.message))
                            
                            # Excel agent için metadata oluştur
                            response_metadata = {
                                "agent_type": "excel"
                            }
                            db.save_chat_message(
                                chat_id, 
                                user.tenant_id, 
                                ChatMessage(sender="ai", text=response_text, metadata=response_metadata)
                            )
                            
                            return ChatResponse(
                                chat_id=chat_id,
                                response_message=response_text,
                                active_context_files=request.context_files or [],
                                response_metadata=response_metadata
                            )
                        else:
                            print(f"⚠️ Excel Agent: Seçilen dosya desteklenen formatta değil (xlsx, xls, csv): '{file_record.name}'")
                    else:
                        print(f"⚠️ Excel Agent: Dosya bulunamadı")
                
                elif len(excel_files) == 2:
                    # İki Excel dosyası karşılaştırması
                    file1 = db.get_file_by_id(user.tenant_id, excel_files[0].id)
                    file2 = db.get_file_by_id(user.tenant_id, excel_files[1].id)
                    
                    if file1 and file2:
                        file1_name_lower = file1.name.lower()
                        file2_name_lower = file2.name.lower()
                        
                        valid_extensions = ('.xlsx', '.xls', '.csv')
                        if file1_name_lower.endswith(valid_extensions) and file2_name_lower.endswith(valid_extensions):
                            print(f"📊 Excel Agent: İki dosya karşılaştırması başlatılıyor - '{file1.name}' ve '{file2.name}'")
                            file1_bytes = _get_file_bytes(file1, user, storage)
                            file2_bytes = _get_file_bytes(file2, user, storage)
                            
                            # GÜNCELLEME BURADA: file1_name ve file2_name eklendi
                            response_text = compare_excel_files(
                                file1_bytes=file1_bytes,
                                file2_bytes=file2_bytes,
                                question=request.message,
                                model_name=model_name,
                                file1_name=file1.name,  # <--- Dosya adı 1
                                file2_name=file2.name   # <--- Dosya adı 2
                            )
                            
                            chat_id = request.chat_id or db.create_chat_session(
                                user.id, user.tenant_id, request.message[:40] + "..."
                            ).id
                            db.save_chat_message(chat_id, user.tenant_id, ChatMessage(sender="user", text=request.message))
                            
                            # Excel agent için metadata oluştur
                            response_metadata = {
                                "agent_type": "excel"
                            }
                            db.save_chat_message(
                                chat_id, 
                                user.tenant_id, 
                                ChatMessage(sender="ai", text=response_text, metadata=response_metadata)
                            )
                            
                            return ChatResponse(
                                chat_id=chat_id,
                                response_message=response_text,
                                active_context_files=request.context_files or [],
                                response_metadata=response_metadata
                            )
                        else:
                            print(f"⚠️ Excel Agent: Seçilen dosyalardan biri desteklenen formatta değil")
                    else:
                        print(f"⚠️ Excel Agent: Dosyalardan biri veya ikisi bulunamadı")
                else:
                    print(f"⚠️ Excel Agent: {len(excel_files)} dosya seçilmiş. Tek veya iki dosya bekleniyor.")
            else:
                print(f"⚠️ Excel Agent: Context dosyası seçilmemiş.")
        except Exception as e:
            print(f"❌ Excel Agent hatası: {e}")
            tb_module.print_exc()
    
    # Presentation agent için özel işleme
    if agent_type == "presentation":
        try:
            from app.services.presentation_agent_service import (
                analyze_presentation_requirements,
                generate_presentation_content,
                create_presentation_file,
                extract_context_info
            )
            
            print(f"📽️ Presentation Agent: Sunum hazırlama isteği alındı")
            
            # Önceki mesajları kontrol et - eksik bilgi toplama aşamasında mıyız?
            previous_topic = request.message
            missing_fields_info = None
            if request.chat_id:
                previous_messages = db.get_chat_messages(chat_id=request.chat_id, tenant_id=user.tenant_id)
                # Son AI mesajını kontrol et
                for msg in reversed(previous_messages):
                    if msg.sender == "ai" and msg.metadata:
                        metadata = msg.metadata
                        if metadata.get("agent_type") == "presentation" and metadata.get("presentation_state") == "collecting_info":
                            # Eksik bilgi toplama aşamasındayız
                            missing_fields_info = metadata.get("missing_fields", [])
                            
                            # İlk kullanıcı mesajını bul (orijinal konu)
                            for prev_msg in previous_messages:
                                if prev_msg.sender == "user":
                                    previous_topic = prev_msg.text
                                    break
                            
                            # Tüm kullanıcı cevaplarını topla (AI mesajından sonraki tüm user mesajları)
                            user_responses = []
                            found_ai_question = False
                            for prev_msg in reversed(previous_messages):
                                if prev_msg.sender == "ai" and prev_msg.metadata and prev_msg.metadata.get("agent_type") == "presentation":
                                    found_ai_question = True
                                elif found_ai_question and prev_msg.sender == "user":
                                    user_responses.insert(0, prev_msg.text)
                            
                            # Yeni mesajı da ekle
                            user_responses.append(request.message)
                            
                            # Daha net bir format oluştur
                            answers_text = "\n".join([f"- {resp}" for resp in user_responses])
                            combined_topic = f"""ORİJİNAL KONU:
{previous_topic}

KULLANICININ VERDİĞİ CEVAPLAR:
{answers_text}

NOT: Kullanıcı yukarıdaki soruları cevapladı. Artık yeterli bilgiye sahipsin, sunum yapısını oluşturabilirsin."""
                            previous_topic = combined_topic
                            print(f"📽️ Presentation Agent: Eksik bilgiler toplandı ({len(user_responses)} cevap), tekrar analiz yapılıyor")
                            break
            
            # Context bilgilerini çıkar
            context_info = ""
            if request.context_files and len(request.context_files) > 0:
                context_info = extract_context_info(
                    [{"id": f.id, "type": f.type, "name": f.name} for f in request.context_files],
                    db, storage, user
                )
            
            # Sunum gereksinimlerini analiz et
            user_answered = missing_fields_info is not None
            analysis_result = analyze_presentation_requirements(
                topic=previous_topic,
                context_info=context_info,
                model_name=model_name,
                user_answered_questions=user_answered
            )
            
            chat_id = request.chat_id or db.create_chat_session(
                user.id, user.tenant_id, request.message[:40] + "..."
            ).id
            db.save_chat_message(chat_id, user.tenant_id, ChatMessage(sender="user", text=request.message))
            
            if analysis_result.get("status") == "missing_info":
                # Eksik bilgiler var, kullanıcıya sor
                missing_fields = analysis_result.get("missing_fields", [])
                questions_text = "Sunum hazırlamak için aşağıdaki bilgilere ihtiyacım var:\n\n"
                for idx, field in enumerate(missing_fields, 1):
                    questions_text += f"{idx}. {field.get('question', '')}\n"
                
                questions_text += "\nLütfen bu soruları yanıtlayın, ben de sunumunuzu hazırlayayım."
                
                # Metadata'ya eksik alanları ekle
                response_metadata = {
                    "agent_type": "presentation",
                    "missing_fields": missing_fields,
                    "presentation_state": "collecting_info"
                }
                
                # ChatMessage'a metadata ekle
                db.save_chat_message(chat_id, user.tenant_id, ChatMessage(
                    sender="ai", 
                    text=questions_text,
                    metadata=response_metadata
                ))
                
                return ChatResponse(
                    chat_id=chat_id,
                    response_message=questions_text,
                    active_context_files=request.context_files or [],
                    response_metadata=response_metadata
                )
            
            elif analysis_result.get("status") == "ready":
                # Yeterli bilgi var, sunum içeriğini oluştur
                structure = analysis_result.get("presentation_structure", {})
                content_result = generate_presentation_content(structure, model_name)
                
                # Eğer content_result boşsa, structure'dan direkt kullan
                slides_to_use = content_result.get("slides", [])
                if not slides_to_use:
                    print(f"⚠️ İçerik oluşturulamadı, structure'dan slaytlar kullanılıyor")
                    # Structure'daki slaytları formatla
                    structure_slides = structure.get("slides", [])
                    slides_to_use = []
                    for slide in structure_slides:
                        slide_content = slide.get("content", [])
                        bullet_points = []
                        for content_item in slide_content:
                            if isinstance(content_item, str):
                                bullet_points.append({
                                    "point": content_item,
                                    "description": ""
                                })
                        slides_to_use.append({
                            "slide_number": slide.get("slide_number", len(slides_to_use) + 1),
                            "slide_type": slide.get("slide_type", "content"),
                            "title": slide.get("title", ""),
                            "bullet_points": bullet_points
                        })
                
                # Sunum dosyasını oluştur
                presentation_title = structure.get("title", "Sunum")
                presentation_bytes = create_presentation_file(
                    {
                        "title": presentation_title,
                        "subtitle": structure.get("subtitle", ""),
                        "slides": slides_to_use
                    },
                    title=presentation_title
                )
                
                # Sunumu storage'a kaydet
                from datetime import datetime
                from app.schemas.file import FileCreate
                import uuid
                
                safe_title = "".join(c for c in presentation_title if c.isalnum() or c in (' ', '-', '_')).strip()[:50]
                filename = f"{safe_title}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pptx"
                
                # Dosyayı storage'a yükle
                unique_filename = f"{uuid.uuid4()}_{filename}"
                storage_path = storage.upload_file(
                    file_obj=io.BytesIO(presentation_bytes),
                    tenant_id=user.tenant_id,
                    file_name=unique_filename
                )
                
                # Dosya kaydını oluştur
                file_data = FileCreate(
                    name=filename,
                    folder_id=None,  # Root klasöre kaydet
                    content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    size=len(presentation_bytes),
                    owner_id=user.id,
                    tenant_id=user.tenant_id,
                    storage_path=storage_path,
                    created_at=datetime.now()
                )
                
                file_record = db.create_file_record(file_data)
                
                # Slayt sayısını kontrol et
                slides_count = len(slides_to_use)
                
                success_message = f"✅ Sunumunuz hazır!\n\n"
                success_message += f"**Başlık:** {presentation_title}\n"
                success_message += f"**Slayt Sayısı:** {slides_count}\n\n"
                success_message += f"Sunum dosyası '{file_record.name}' olarak kaydedildi. Dosyalar bölümünden indirebilirsiniz."
                
                print(f"📽️ Sunum kaydedildi: {file_record.name}, {slides_count} slayt, {len(presentation_bytes)} bytes")
                
                response_metadata = {
                    "agent_type": "presentation",
                    "presentation_state": "completed",
                    "file_id": file_record.id,
                    "file_name": file_record.name
                }
                
                # ChatMessage'a metadata ekle
                db.save_chat_message(chat_id, user.tenant_id, ChatMessage(
                    sender="ai", 
                    text=success_message,
                    metadata=response_metadata
                ))
                
                return ChatResponse(
                    chat_id=chat_id,
                    response_message=success_message,
                    active_context_files=request.context_files or [],
                    response_metadata=response_metadata
                )
            else:
                # Hata durumu
                error_message = analysis_result.get("message", "Sunum hazırlanırken bir hata oluştu.")
                db.save_chat_message(chat_id, user.tenant_id, ChatMessage(sender="ai", text=error_message))
                
                return ChatResponse(
                    chat_id=chat_id,
                    response_message=error_message,
                    active_context_files=request.context_files or []
                )
                
        except Exception as e:
            print(f"❌ Presentation Agent hatası: {e}")
            tb_module.print_exc()
            # Hata durumunda normal işleme devam et
    
    # Selamlaşma/hal hatır kontrolü (en önce - nazik cevap verilmeli)
    if is_greeting_query(request.message):
        print(f"👋 Selamlaşma sorgusu tespit edildi: '{request.message}' - Nazik cevap verilecek")
        chat_id = request.chat_id or db.create_chat_session(user.id, user.tenant_id, request.message[:40] + "...").id
        db.save_chat_message(chat_id, user.tenant_id, ChatMessage(sender="user", text=request.message))
        
        # Nazik selamlaşma cevabı
        greeting_response = get_greeting_response(request.message)
        db.save_chat_message(chat_id, user.tenant_id, ChatMessage(sender="ai", text=greeting_response))
        
        return ChatResponse(
            chat_id=chat_id,
            response_message=greeting_response,
            token_usage_stats=None
        )
    
    # Off-topic sorgu kontrolü (dosya taraması yapmadan önce)
    # NOT: Eğer context_files varsa (kullanıcı dosya/klasör seçmişse), off-topic kontrolünü atla
    # çünkü bu durumda soru kesinlikle platform ile ilgilidir
    # NOT: Özel agent'lar (excel, presentation) için de off-topic kontrolünü atla (bunlar agent'ların görevidir)
    has_context = request.context_files and len(request.context_files) > 0
    is_special_agent = agent_type in ["excel", "presentation"]
    if not has_context and not is_special_agent and is_off_topic_query(request.message):
        print(f"⚠️ Off-topic sorgu tespit edildi: '{request.message}' - Dosya taraması yapılmayacak")
        chat_id = request.chat_id or db.create_chat_session(user.id, user.tenant_id, request.message[:40] + "...").id
        db.save_chat_message(chat_id, user.tenant_id, ChatMessage(sender="user", text=request.message))
        
        # Off-topic mesajı kaydet
        off_topic_response = "Üzgünüm, bu tür genel sohbet sorularını yanıtlayamam. Lütfen Knowvex ile ilgili sorular sorun. Örneğin: 'Dosyalarda X konusunu ara', 'Y projesi hakkında bilgi ver', 'Z raporunu özetle', 'Bugün kaç mail geldi?' gibi."
        db.save_chat_message(chat_id, user.tenant_id, ChatMessage(sender="ai", text=off_topic_response))
        
        return ChatResponse(
            chat_id=chat_id,
            response_message=off_topic_response,
            token_usage_stats=None
        )
    
    # Yardım/destek sorgu kontrolü (dosya taraması yapmadan önce)
    if is_help_or_support_query(request.message):
        print(f"ℹ️ Yardım/destek sorgusu tespit edildi: '{request.message}' - Dosya taraması yapılmayacak, direkt cevap verilecek")
        chat_id = request.chat_id or db.create_chat_session(user.id, user.tenant_id, request.message[:40] + "...").id
        db.save_chat_message(chat_id, user.tenant_id, ChatMessage(sender="user", text=request.message))
        
        # Yardım cevabı oluştur
        help_response = get_help_response(request.message)
        db.save_chat_message(chat_id, user.tenant_id, ChatMessage(sender="ai", text=help_response))
        
        return ChatResponse(
            chat_id=chat_id,
            response_message=help_response,
            token_usage_stats=None
        )
    
    # Token tracking başlat
    token_tracker = TokenTracker()
    print("\n🔢 TOKEN TRACKING BAŞLATILDI - Tüm LLM çağrıları izlenecek\n")
    
    chat_id = request.chat_id or db.create_chat_session(user.id, user.tenant_id, request.message[:40] + "...").id
    context_memory = get_context_memory_for_chat(chat_id)
    
    db.save_chat_message(chat_id, user.tenant_id, ChatMessage(sender="user", text=request.message))
    
    response_data: Dict[str, Any] = {}
    
    # filtered_context_files'i başlangıçta boş liste olarak başlat
    filtered_context_files = []
    
    # DEBUG: Gelen context_files bilgisini logla
    print(f"🔍 DEBUG: Gelen context_files = {request.context_files} (type: {type(request.context_files)}, is None: {request.context_files is None}, len: {len(request.context_files) if request.context_files is not None else 'N/A'})")
    print(f"🔍 DEBUG: Mevcut bağlam hafızası: {len(context_memory.get_file_ids())} dosya, {len(context_memory.get_folder_ids())} klasör")
    
    # Context dosyaları için erişim kontrolü
    # NOT: request.context_files None olabilir (bağlam belirtilmemiş) veya boş liste olabilir (bağlam kaldırılmış)
    # Boş liste açıkça gönderildiyse, bağlamı temizle
    if request.context_files is not None:
        if len(request.context_files) == 0:
            # Kullanıcı tüm bağlamı kaldırmış - bağlam hafızasını temizle
            context_memory.clear()
            print("🗑️ Kullanıcı tüm bağlamı kaldırdı. Bağlam hafızası temizlendi.")
        else:
            # Kullanıcının erişebileceği dosya ve klasörleri al
            accessible_files = get_all_accessible_files_for_user(db, user)
            accessible_file_ids = {file.id for file in accessible_files}
            accessible_folder_ids = set()
            
            # Admin ise tüm klasörlere erişebilir
            if user.role == "Admin":
                all_folders = db.get_all_folders_for_tenant(user.tenant_id)
                accessible_folder_ids = {folder.id for folder in all_folders}
            else:
                # Rol bazlı klasör erişimleri
                user_role = db.get_role_by_name(tenant_id=user.tenant_id, role_name=user.role)
                if user_role:
                    accessible_folder_ids = set(user_role.allowed_folders or [])
            
            # Erişilebilir context dosyalarını filtrele
            filtered_context_files = []
            for context_item in request.context_files:
                if context_item.type == "file":
                    # Dosya erişim kontrolü
                    file_record = db.get_file_by_id(user.tenant_id, context_item.id)
                    if file_record:
                        is_owner = file_record.owner_id == user.id  # Kullanıcının kendi yüklediği dosya
                        is_accessible = context_item.id in accessible_file_ids  # Rolünde tanımlı dosya
                        # Kullanıcı kendi dosyasına veya rolünde tanımlı dosyaya erişebilir, Admin her şeye erişebilir
                        if is_owner or is_accessible or user.role == "Admin":
                            filtered_context_files.append(context_item)
                            if is_owner:
                                print(f"✅ Kullanıcının kendi dosyası bağlama eklendi: '{context_item.name}'")
                        else:
                            print(f"⚠️ Erişim reddedildi: Kullanıcı '{user.email}' '{context_item.name}' dosyasına erişemiyor.")
                    else:
                        print(f"⚠️ Dosya bulunamadı: '{context_item.name}' (ID: {context_item.id})")
                elif context_item.type == "folder":
                    # Klasör erişim kontrolü
                    folder_record = None
                    all_folders = db.get_all_folders_for_tenant(user.tenant_id)
                    for folder in all_folders:
                        if folder.id == context_item.id:
                            folder_record = folder
                            break
                    
                    if folder_record:
                        is_accessible = context_item.id in accessible_folder_ids or user.role == "Admin"
                        if is_accessible:
                            filtered_context_files.append(context_item)
                        else:
                            print(f"⚠️ Erişim reddedildi: Kullanıcı '{user.email}' '{context_item.name}' klasörüne erişemiyor.")
                    else:
                        print(f"⚠️ Klasör bulunamadı: '{context_item.name}' (ID: {context_item.id})")
                elif context_item.type == "database":
                    # YENİ: Veritabanı erişim kontrolü
                    try:
                        from app.api.v1.databases import get_database_connection
                        db_connection = get_database_connection(context_item.id, user)
                        if db_connection:
                            filtered_context_files.append(context_item)
                            print(f"✅ Veritabanı bağlantısı bağlama eklendi: '{context_item.name}' ({context_item.db_type})")
                        else:
                            print(f"⚠️ Veritabanı bağlantısı bulunamadı: '{context_item.name}' (ID: {context_item.id})")
                    except Exception as e:
                        print(f"⚠️ Veritabanı bağlantısı kontrol edilirken hata: {e}")
            
            if filtered_context_files:
                context_memory.set_context(filtered_context_files)
                print(f"✅ {len(filtered_context_files)} adet bağlam dosyası/klasörü erişim kontrolünden geçti ve eklendi.")
            else:
                # Erişilebilir dosya yoksa, bağlamı temizle (kullanıcı bağlam kaldırmış olabilir)
                context_memory.clear()
                print(f"⚠️ Hiçbir bağlam dosyası/klasörü erişilebilir değil. Bağlam hafızası temizlendi.")
    
    is_general_search = not context_memory.has_context()

    if is_general_search:
        print("Bağlam belirtilmedi, dosya adıyla hızlı arama yapılıyor...")
        all_files = get_all_accessible_files_for_user(db, user)
        best_match_file = None
        highest_score = 0.9  
        for file in all_files:
            score = calculate_filename_match_score(request.message, file.name)
            if score > highest_score:
                highest_score = score
                best_match_file = file
        if best_match_file:
            print(f"💡 Hızlı arama başarılı! '{best_match_file.name}' dosyası bağlam olarak ayarlandı.")
            file_context = ActiveContextFile(id=best_match_file.id, name=best_match_file.name, type="file")
            context_memory.set_context([file_context])
            db.save_chat_message(
                chat_id, 
                user.tenant_id, 
                ChatMessage(sender="system", text=f"Sorunuzla ilgili olabilecek '{best_match_file.name}' dosyası otomatik olarak bağlama eklendi.")
            )
        else:
            print("Dosya adıyla güçlü bir eşleşme bulunamadı, genel vektör aramasına geçiliyor.")
    
    is_general_search = not context_memory.has_context()
        
    search_file_ids, search_folder_ids = context_memory.get_file_ids(), context_memory.get_folder_ids()
    if search_folder_ids:
        for folder_id in search_folder_ids:
            try:
                file_ids_in_folder = db.get_all_file_ids_in_folder_recursive(tenant_id=user.tenant_id, folder_id=folder_id, user=user)
                search_file_ids.update(file_ids_in_folder)
            except Exception as e:
                print(f"Klasör içeriği alınırken hata: {e}")

    if is_general_search and user.role != "Admin":
        print(f"Bağlamsız arama. Kullanıcı '{user.email}' (Rol: {user.role}) için yetki filtresi uygulanıyor.")
        try:
            if 'all_files' not in locals():
                 all_files = get_all_accessible_files_for_user(db, user)
            allowed_file_ids = {file.id for file in all_files}
            search_file_ids = allowed_file_ids
            if not search_file_ids:
                print(f"Kullanıcı '{user.email}' hiçbir dosyaya erişemiyor. Arama engellendi.")
                response_data = {
                    "response_message": "Yetkiniz olan herhangi bir dosya bulunamadığı için genel arama yapamıyorum. Lütfen belirli bir dosya veya klasörü @-etiketleyerek tekrar deneyin.",
                    "source_context": "Yetki Engeli",
                    "token_usage": {}
                }
        except Exception as e:
            print(f"Kullanıcı yetkileri alınırken hata oluştu: {e}")
            raise HTTPException(status_code=500, detail=f"Arama yetkileri hesaplanırken hata oluştu: {str(e)}")

    # YENİ: Veritabanı bağlamı kontrolü
    database_context = None
    # Önce filtered_context_files'de veritabanı var mı kontrol et
    if filtered_context_files:
        for context_item in filtered_context_files:
            if context_item.type == "database":
                try:
                    from app.api.v1.databases import get_database_connection
                    from app.database_connectors import get_database_connector
                    from app.services.database_query_service import query_database
                    
                    db_connection = get_database_connection(context_item.id, user)
                    if db_connection:
                        connector = get_database_connector(db_connection.type)
                        if connector.connect(db_connection.connection_string):
                            database_context = {
                                "connection": db_connection,
                                "connector": connector
                            }
                            print(f"🔗 Veritabanı bağlantısı kuruldu: {db_connection.name} ({db_connection.type})")
                            break
                except Exception as e:
                    print(f"⚠️ Veritabanı bağlantısı kurulurken hata: {e}")
    
    # Eğer filtered_context_files'de veritabanı yoksa, context_memory'den kontrol et
    if not database_context:
        for context_item in context_memory.get_context():
            if context_item.type == "database":
                try:
                    from app.api.v1.databases import get_database_connection
                    from app.database_connectors import get_database_connector
                    from app.services.database_query_service import query_database
                    
                    db_connection = get_database_connection(context_item.id, user)
                    if db_connection:
                        connector = get_database_connector(db_connection.type)
                        if connector.connect(db_connection.connection_string):
                            database_context = {
                                "connection": db_connection,
                                "connector": connector
                            }
                            print(f"🔗 Veritabanı bağlantısı kuruldu: {db_connection.name} ({db_connection.type})")
                            break
                except Exception as e:
                    print(f"⚠️ Veritabanı bağlantısı kurulurken hata: {e}")
    
    # Eğer veritabanı bağlamı varsa, veritabanı sorgulama yap
    if database_context:
        print("\n" + "="*50 + "\nVERİTABANI SORGULAMA MODU\n" + "="*50)
        try:
            db_result = query_database(
                question=request.message,
                db_connector=database_context["connector"],
                model_name=model_name
            )
            
            # Veritabanı sonucunu response'a ekle
            source_context = f"Veritabanı: {database_context['connection'].name}"
            if db_result.get("sql_query"):
                source_context += f" | SQL: {db_result['sql_query']}"
            
            response_data = {
                "response_message": db_result["answer"],
                "source_context": source_context,
                "token_usage": {
                    "input_tokens": 0,  # Veritabanı sorgusu için token tracking yapılmadı (basit tutuldu)
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "breakdown": [],
                    "estimated_cost_usd": 0.0,
                    "estimated_cost_tl": 0.0
                }
            }
            
            # Bağlantıyı kapat
            database_context["connector"].close()
        except Exception as e:
            print(f"❌ Veritabanı sorgusu hatası: {e}")
            response_data = {
                "response_message": f"Veritabanı sorgusu sırasında bir hata oluştu: {str(e)}",
                "source_context": f"Veritabanı: {database_context['connection'].name}",
                "token_usage": {}
            }
            if database_context and database_context.get("connector"):
                database_context["connector"].close()
    
    is_single_file_context = len(context_memory.get_file_ids()) == 1 and not context_memory.get_folder_ids()

    def retrieve_docs(query: str) -> List[Document]:
        print(f"🔀 Hibrit arama yapılıyor (LocalGPT tarzı - Vector + BM25 + RRF): {query[:100]}...")
        
        if is_general_search:
             # Eğer liste sorusuysa daha da derin kaz
            search_limit = 500 if is_list_intent(request.message) else 300
        else:
            search_limit = 150
        
        # YENİ: Hibrit arama kullan (LocalGPT'in yaklaşımı)
        chunks_dict = vector_service.hybrid_search_similar_chunks(
            tenant_id=user.tenant_id,
            query=query,
            db=db,
            limit=search_limit,
            filter_file_ids=list(search_file_ids) if search_file_ids else None,
            retrieval_mode="hybrid"  # "hybrid", "vector", "bm25"
        )
        
        # Mail dosyalarını filtrele (mail arama ayrı bir endpoint'te yapılıyor)
        filtered_chunks = [
            chunk for chunk in chunks_dict 
            if not chunk.get("source_file_id", "").startswith("mail_")
        ]
        
        print(f"📊 Hibrit arama: {len(chunks_dict)} chunk bulundu, {len(filtered_chunks)} chunk mail olmayan dosyalardan (mail dosyaları filtrelendi)")
        
        return [Document(page_content=chunk.get("text"), metadata={
            "source_file_name": chunk.get("source_file_name"),
            "source_file_id": chunk.get("source_file_id"),
            "similarity_score": chunk.get("rrf_score", chunk.get("hybrid_score", chunk.get("similarity_score", 0.0))),
            "vector_score": chunk.get("vector_score", 0.0),
            "bm25_score": chunk.get("bm25_score", 0.0),
            "rrf_score": chunk.get("rrf_score", 0.0)
        }) for chunk in filtered_chunks]

    try:
        if not response_data:
            print("\n" + "="*50 + "\nADIM 1: BİLGİ GETİRME (RETRIEVAL)\n" + "="*50)

            if is_single_file_context:
                print("📄 Tek dosya bağlamı algılandı. Performans için HyDE adımı atlanıyor.")
                retriever_chain = RunnableLambda(retrieve_docs)
            else:
                # HyDE için wrapper - model_name ve token_tracker'ı closure ile geçir
                def hyde_wrapper(question: str):
                    return create_hypothetical_document_for_query_wrapper(question, model_name, token_tracker)
                retriever_chain = (RunnableLambda(hyde_wrapper) | RunnableLambda(retrieve_docs))
            
            retrieved_chunks = retriever_chain.invoke(request.message)
            
            is_list_query = is_list_intent(request.message)
            if is_list_query:
                print("🔍 Liste talebi algılandı. Eksiksiz liste için reranking ile en alakalı chunk'lar seçilecek...")
                # Liste sorularında da reranking yap ama daha fazla chunk seçmesini iste
                is_champion_found = False
                # RRF score'a göre sırala ve en iyi 300 chunk'ı reranking'e gönder
                sorted_chunks = sorted(retrieved_chunks, key=lambda x: x.metadata.get('rrf_score', x.metadata.get('similarity_score', 0.0)), reverse=True)
                top_chunks_for_rerank = sorted_chunks[:300]  # Reranking için en iyi 300 chunk
                
                # Cross-Encoder reranking dene (LocalGPT tarzı - daha hızlı)
                cross_encoder_result = rerank_chunks_with_cross_encoder(
                    top_chunks_for_rerank,
                    request.message,
                    top_k=200  # Liste soruları için daha fazla
                )
                
                if cross_encoder_result is not None:
                    # Cross-Encoder başarılı
                    final_chunks = cross_encoder_result
                    print(f"✅ Cross-Encoder reranking kullanıldı: {len(final_chunks)} chunk seçildi")
                else:
                    # Fallback: LLM reranking
                    final_chunks = rerank_chunks_with_llm_wrapper(top_chunks_for_rerank, request.message, model_name, token_tracker, is_list_query=True)
                    print(f"✅ LLM reranking kullanıldı: {len(final_chunks)} chunk seçildi")
                
                print(f"📋 Liste sorusu için {len(final_chunks)} chunk kullanılacak (reranking sonrası).")
            else:
                potential_final_chunks, is_champion_found = identify_and_filter_high_confidence_document(retrieved_chunks, request.message)
                
                if is_single_file_context and is_champion_found:
                    print("📄 Tek dosya bağlamı algılandı. En iyi sonucu sağlamak için yeniden sıralayıcı (reranker) zorunlu kılındı.")
                    is_champion_found = False

                if is_champion_found:
                    final_chunks = potential_final_chunks
                else:
                    # Cross-Encoder reranking dene (LocalGPT tarzı - daha hızlı)
                    if len(potential_final_chunks) > 50:
                        # Çok fazla chunk varsa Cross-Encoder kullan (daha hızlı)
                        cross_encoder_result = rerank_chunks_with_cross_encoder(
                            potential_final_chunks,
                            request.message,
                            top_k=50
                        )
                        
                        if cross_encoder_result is not None:
                            # Cross-Encoder başarılı
                            final_chunks = cross_encoder_result
                            print(f"✅ Cross-Encoder reranking kullanıldı: {len(final_chunks)} chunk seçildi")
                        else:
                            # Fallback: LLM reranking
                            final_chunks = rerank_chunks_with_llm_wrapper(potential_final_chunks, request.message, model_name, token_tracker)
                            print(f"✅ LLM reranking kullanıldı: {len(final_chunks)} chunk seçildi")
                    else:
                        # Az chunk varsa LLM reranking (daha esnek)
                        final_chunks = rerank_chunks_with_llm_wrapper(potential_final_chunks, request.message, model_name, token_tracker)
                        print(f"✅ LLM reranking kullanıldı: {len(final_chunks)} chunk seçildi")
            
            # DEBUG: Final chunk'ların içeriğini kontrol et
            if final_chunks:
                print(f"DEBUG: Final chunks içeriği örnekleri:")
                for i, chunk in enumerate(final_chunks[:3]):  # İlk 3 chunk'ı göster
                    print(f"  Chunk {i+1}: {chunk.page_content[:200]}...")
            
            print("\n" + "="*50 + "\nADIM 3: YANIT ÜRETİMİ (GENERATION)\n" + "="*50)

            if is_champion_found and final_chunks:
                 print(f"✨ ODAKLANMA MODU AKTİF: Yanıt, sadece şampiyon belgeden gelen {len(final_chunks)} chunk ile oluşturulacak.")
            
            if not final_chunks:
                 print("UYARI: Yanıt üretimi için HİÇ chunk bulunamadı. Muhtemelen alakasızdı.")

            # RAG prompt template'i prompts.py'den import edildi
            # Llama modeli için özel kısa cevap talimatı ekle
            rag_prompt_template_str = RAG_PROMPT_TEMPLATE
            
            # Llama modeli için kısa ve öz cevap talimatı ekle
            if model_name.lower() == "llama":
                llama_instruction = """

**LLAMA MODELİ İÇİN ÖZEL TALİMAT (ÇOK ÖNEMLİ):**
- CEVAPLARINI MUTLAKA TÜRKÇE VER
- Kısa, öz ve direkt cevap ver - gereksiz açıklama yapma
- "I'm a corporate memory assistant" gibi İngilizce girişler YASAKTIR
- Soruyu tekrar yazma, direkt cevaba başla
- Örnek: "Fatura tarihi nedir?" sorusuna → "Fatura tarihi 11.01.2025'tir." gibi kısa cevap ver
- "nedir", "ne zaman", "kaç" gibi basit sorulara tek cümlelik cevap ver
- Liste sorularında bile her öğeyi kısa tut
- ASLA İngilizce cevap verme, MUTLAKA Türkçe cevap ver
- Gereksiz detaylar, açıklamalar, örnekler verme - sadece sorulan soruya cevap ver

"""
                # Prompt'un sonuna ekle (KRİTİK CEVAP KURALLARI bölümünden önce)
                rag_prompt_template_str = rag_prompt_template_str.replace(
                    "**KRİTİK CEVAP KURALLARI:**",
                    llama_instruction + "**KRİTİK CEVAP KURALLARI:**"
                )
                print("🇹🇷 Llama modeli için kısa ve öz Türkçe cevap talimatı eklendi")
            
            rag_prompt = ChatPromptTemplate.from_template(rag_prompt_template_str)
            
            def format_docs_for_prompt(docs: List[Document]) -> str:
                if not docs: return "Kullanıcının sorusuyla ilgili spesifik bir belge bulunamadı."
                
                # Liste soruları için daha fazla chunk gönder ama sınırlı (eksiksiz liste için)
                is_list_query = is_list_intent(request.message)
                question_lower = request.message.lower()
                has_company_name = any(word in question_lower for word in ['firma', 'şirket', 'tedarikçi', 'müşteri', 'supplier', 'vendor', 'company', 'client', 'customer'])
                has_document_type = any(word in question_lower for word in ['teklif', 'sözleşme', 'fatura', 'po', 'purchase order', 'offer', 'invoice', 'contract'])
                
                if is_list_query:
                    # Tedarikçi/firma soruları için daha fazla chunk gönder
                    is_supplier_query = has_company_name
                    is_name_list_query = any(word in question_lower for word in ['isimleri', 'isimler', 'kimler', 'hangi.*aday', 'hangi.*kisi', 'nedir.*isim'])
                    # İsim listesi soruları için daha fazla chunk gerekli (eksiksiz liste için)
                    max_chunks = 300 if is_supplier_query else (250 if is_name_list_query else 200)  # İsim listesi soruları için max 250 chunk
                    chunks_to_send = docs[:max_chunks] if len(docs) > max_chunks else docs
                    print(f"📋 Liste sorusu algılandı - {len(chunks_to_send)} chunk gönderiliyor (reranking sonrası, eksiksiz liste için)...")
                elif has_company_name and has_document_type:
                    # Firma ismi ve belge türü içeren detay soruları için (örn: "SILA firmasına verilen teklif detayları")
                    max_chunks = 80  # Firma ismi ve belge türü içeren sorular için daha fazla chunk
                    chunks_to_send = docs[:max_chunks] if len(docs) > max_chunks else docs
                    print(f"📄 Firma/belge detay sorusu algılandı - {len(chunks_to_send)} chunk gönderiliyor...")
                elif has_company_name:
                    # Sadece firma ismi içeren sorular için
                    max_chunks = 60  # Firma ismi içeren sorular için
                    chunks_to_send = docs[:max_chunks] if len(docs) > max_chunks else docs
                    print(f"📄 Firma detay sorusu algılandı - {len(chunks_to_send)} chunk gönderiliyor...")
                else:
                    # "kaç adet", "toplamda kaç" gibi sayısal sorular için daha fazla chunk gerekli
                    is_count_query = any(word in question_lower for word in ['kac', 'toplam', 'adet', 'sayi', 'count', 'total', 'how many'])
                    max_chunks = 150 if is_count_query else 100  # Sayısal sorular için max 150 chunk
                    chunks_to_send = docs[:max_chunks] if len(docs) > max_chunks else docs
                    print(f"Yanıt üretimi için LLM'e {len(chunks_to_send)} adet chunk gönderiliyor...")
                
                # Her chunk'a numara ekle (LLM'in takip edebilmesi için)
                formatted_chunks = []
                for idx, doc in enumerate(chunks_to_send, 1):
                    chunk_text = f"--- Alıntı #{idx} (Kaynak: {doc.metadata.get('source_file_name', 'Bilinmiyor')}) ---\n{doc.page_content}"
                    formatted_chunks.append(chunk_text)
                
                return "\n\n".join(formatted_chunks)
            
            # Model seçimine göre LLM oluştur
            # SMART MODE: Eğer sorgu basitse ve kullanıcı özel bir model zorlamadıysa (varsayılan gemini ise), ucuz modeli kullan
            is_simple = is_simple_query(request.message)
            if is_simple and model_name == "gemini":
                print(f"🚀 Basit sorgu algılandı, maliyet optimizasyonu için UCUZ MODEL (Gemini Flash) kullanılıyor.")
                selected_llm = get_cheap_llm()
                # Metadata için model adını güncelle (raporlama için)
                if hasattr(selected_llm, 'model_name'):
                    model_name = f"{selected_llm.model_name} (Smart Mode)"
            else:
                selected_llm = get_llm_for_model(model_name)
            
            print(f"🔗 Final RAG chain'i {model_name} modeli ile oluşturuluyor...")
            
            # Chain'i oluştur - model seçimine göre
            rag_chain = ({"context": lambda x: format_docs_for_prompt(x["chunks"]), "question": lambda x: x["question"]} | rag_prompt | selected_llm)
            
            # Chain'i çağır
            ai_full_response = rag_chain.invoke({"chunks": final_chunks, "question": request.message})
            
            # LLM yanıtını logla (debug için)
            ai_response_text = ai_full_response.content if hasattr(ai_full_response, 'content') and ai_full_response.content else ""
            print(f"DEBUG: LLM yanıt içeriği (ilk 500 karakter): {ai_response_text[:500]}")
            
            # Sayısal cevabı kontrol et
            import re
            numbers_in_response = re.findall(r'\d+', ai_response_text)
            if numbers_in_response:
                print(f"DEBUG: LLM yanıtında bulunan sayılar: {numbers_in_response}")
            
            # Aday isimlerini kontrol et (basit bir kontrol)
            if "aday" in ai_response_text.lower():
                # "X aday" veya "Y adayla" gibi kalıpları ara
                candidate_matches = re.findall(r'(\d+)\s*(?:farklı\s*)?(?:aday|kişi|görüş)', ai_response_text, re.IGNORECASE)
                if candidate_matches:
                    print(f"DEBUG: Yanıtta bulunan aday sayısı referansları: {candidate_matches}")
            
            # Final RAG çağrısı için token tracking - metadata'dan al (prompt string'i gerekmez)
            input_tokens, output_tokens = extract_token_usage_from_response(ai_full_response, "Final RAG")
            estimated = not (hasattr(ai_full_response, 'usage_metadata') and ai_full_response.usage_metadata) and not (hasattr(ai_full_response, 'response_metadata') and ai_full_response.response_metadata and ('usage_metadata' in ai_full_response.response_metadata or 'token_usage' in ai_full_response.response_metadata))
            token_tracker.add_usage(
                input_tokens,
                output_tokens,
                "Final RAG (Yanıt Üretimi)",
                estimated=estimated,
                raw_metadata=ai_full_response.response_metadata if hasattr(ai_full_response, 'response_metadata') else None
            )
            
            ai_message_text, source_context_text = "", ""
            source_file_names = []  # Kaynak dosya adlarını sakla
            
            # Llama için cevap temizleme - gereksiz başlık ve açıklamaları kaldır
            def clean_llama_response(text, model_name, question=""):
                """Llama modelinin cevabından gereksiz kısımları temizle ve kısa/öz hale getir"""
                if model_name.lower() != "llama":
                    return text
                
                import re
                
                # Önce KAYNAKLAR bölümünü ayır
                main_text = text
                sources_text = ""
                if "KAYNAKLAR:" in text:
                    parts = text.split("KAYNAKLAR:", 1)
                    main_text = parts[0].strip()
                    sources_text = parts[1].strip()
                
                # İngilizce giriş bloklarını kaldır (daha agresif)
                # "I'm a corporate memory assistant! I'll help you..." gibi tüm blokları kaldır
                main_text = re.sub(r"^I'm a corporate memory assistant[^.]*[.!?].*?To answer your question:", '', main_text, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)
                main_text = re.sub(r"^I'm a corporate memory assistant[^.]*[.!?].*?Please note that[^.]*[.!?]", '', main_text, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)
                
                # İngilizce giriş cümlelerini kaldır (daha kapsamlı)
                english_intros = [
                    r"^I'm a corporate memory assistant[^.!?]*[.!?]",
                    r"^I'll help you with[^.!?]*[.!?]",
                    r"^Please note that[^.!?]*[.!?]",
                    r"^Since there are multiple[^.!?]*[.!?]",
                    r"^Let me[^.!?]*[.!?]",
                    r"^I'll go through[^.!?]*[.!?]",
                    r"^The quote appears to be[^.!?]*[.!?]",
                    r"^This quote appears to be[^.!?]*[.!?]",
                    r"^Based on the provided[^.!?]*[.!?]",
                    r"^According to the[^.!?]*[.!?]",
                    r"^To answer your question[^.!?]*[.!?]",
                    r"^Here are the[^.!?]*[.!?]",
                    r"^Please let me know[^.!?]*[.!?]",
                ]
                for pattern in english_intros:
                    main_text = re.sub(pattern, '', main_text, flags=re.IGNORECASE | re.MULTILINE)
                
                # Soru tekrarını kaldır (başlık formatında)
                main_text = re.sub(r'\*\*.*?sırala.*?\*\*', '', main_text, flags=re.IGNORECASE | re.MULTILINE)
                main_text = re.sub(r'\*\*.*?soru.*?\*\*', '', main_text, flags=re.IGNORECASE | re.MULTILINE)
                
                # "Here are the recent..." gibi açıklama cümlelerini kaldır
                main_text = re.sub(r'Here are the[^:]*:', '', main_text, flags=re.IGNORECASE)
                main_text = re.sub(r'Here are[^:]*:', '', main_text, flags=re.IGNORECASE)
                
                # "**Alıntı #1**", "**Alıntı #2**" gibi İngilizce başlıkları kaldır
                main_text = re.sub(r'\*\*Alıntı\s*#\d+\*\*', '', main_text, flags=re.IGNORECASE)
                main_text = re.sub(r'^Alıntı\s*#\d+:', '', main_text, flags=re.IGNORECASE | re.MULTILINE)
                
                # İngilizce açıklama cümlelerini kaldır
                main_text = re.sub(r'The relevant information includes:', '', main_text, flags=re.IGNORECASE)
                main_text = re.sub(r'The relevant info:', '', main_text, flags=re.IGNORECASE)
                main_text = re.sub(r'Company name:', '', main_text, flags=re.IGNORECASE)
                main_text = re.sub(r'Tax Office:', '', main_text, flags=re.IGNORECASE)
                main_text = re.sub(r'Tax Number:', '', main_text, flags=re.IGNORECASE)
                
                # İlk olarak, "**Cevap:**" gibi başlıkları kaldır
                main_text = re.sub(r'^\*\*Cevap:\*\*\s*\n?', '', main_text, flags=re.IGNORECASE | re.MULTILINE)
                main_text = re.sub(r'^Cevap:\s*\n?', '', main_text, flags=re.IGNORECASE | re.MULTILINE)
                
                # "**Kaynak:**" veya "**Kaynaklar:**" başlıklarını kaldır
                main_text = re.sub(r'\*\*Kaynak[lar]*:\*\*.*?(?=\n\n|KAYNAKLAR:|$)', '', main_text, flags=re.DOTALL | re.IGNORECASE)
                main_text = re.sub(r'Kaynak[lar]*:.*?(?=\n\n|KAYNAKLAR:|$)', '', main_text, flags=re.DOTALL | re.IGNORECASE)
                
                # Soru tekrarlarını ve parantez içi dosya adlarını kaldır
                main_text = re.sub(r'\([^)]*\.(?:pdf|docx?|xlsx?)\)', '', main_text, flags=re.IGNORECASE)
                main_text = re.sub(r'Fatura[^?]*detayını[^?]*yazar[^?]*mısın[^?]*\?', '', main_text, flags=re.IGNORECASE)
                main_text = re.sub(r'Fatura[^.]*detayını[^.]*yazmak[^.]*için[^.]*\.', '', main_text, flags=re.IGNORECASE)
                
                # "Kaynak: ..." satırlarını kaldır
                main_text = re.sub(r'^Kaynak:\s*[^\n]*(?=\n|$)', '', main_text, flags=re.MULTILINE | re.IGNORECASE)
                main_text = re.sub(r'SADECE[^.]*cevabı[^.]*veriyorum[^.]*\.', '', main_text, flags=re.IGNORECASE)
                main_text = re.sub(r'Direkt[^.]*cevaba[^.]*başlıyorum[^.]*\.', '', main_text, flags=re.IGNORECASE)
                
                # "**KRİTİK CEVAP KURALLARI:**" başlığı ve altındaki tüm bloğu kaldır
                main_text = re.sub(r'\*\*KRİTİK CEVAP KURALLARI:\*\*.*?(?=\n\n[^\s\-\*]|\n[^\s\-\*\n]|$)', '', main_text, flags=re.DOTALL | re.IGNORECASE)
                main_text = re.sub(r'KRİTİK CEVAP KURALLARI:.*?(?=\n\n[^\s\-\*]|\n[^\s\-\*\n]|$)', '', main_text, flags=re.DOTALL | re.IGNORECASE)
                
                # "**İSİM LİSTESİ SORULARI İÇİN ÖZEL KURAL**" bloğunu kaldır
                main_text = re.sub(r'\*\*İSİM LİSTESİ.*?(?=\n\n[^\s\-\*]|\n[^\s\-\*\n]|$)', '', main_text, flags=re.DOTALL | re.IGNORECASE)
                
                # "- SADECE cevabı ver" gibi madde işaretli kuralları kaldır
                main_text = re.sub(r'^[\s\-*]*(?:SADECE|ASLA|MUTLAKA|Direkt|Soruyu|TÜM alıntıları|Eğer soru|Firma ismi).*$', '', main_text, flags=re.MULTILINE | re.IGNORECASE)
                
                # Basit sorular için ilk cümleyi al (kısa cevap için)
                # "nedir", "ne zaman", "kaç", "sırala", "listele" gibi sorular için sadece ilk cümleyi tut
                if question:
                    question_lower = question.lower()
                    is_simple_question = any(word in question_lower for word in ['nedir', 'ne zaman', 'kaç', 'kim', 'nerede', 'hangi tarih', 'tarihi nedir', 'sırala', 'listele', 'göster'])
                    
                    if is_simple_question:
                        # İlk cümleyi bul (nokta, soru işareti veya ünlem ile biten)
                        # Veya liste formatında ise ilk birkaç satırı al
                        lines = main_text.split('\n')
                        cleaned_lines = []
                        for line in lines:
                            line = line.strip()
                            if not line:
                                continue
                            # İngilizce satırları atla (Türkçe karakter kontrolü)
                            if re.match(r'^[A-Z][a-z].*[.!?]$', line) and not any(turkish_char in line for turkish_char in ['ç', 'ğ', 'ı', 'ö', 'ş', 'ü', 'Ç', 'Ğ', 'İ', 'Ö', 'Ş', 'Ü']):
                                continue
                            # Türkçe içerik bulundu, ekle
                            cleaned_lines.append(line)
                            # İlk 5-7 satırı al (liste soruları için)
                            if len(cleaned_lines) >= 7:
                                break
                        
                        if cleaned_lines:
                            main_text = '\n'.join(cleaned_lines)
                            print(f"📝 Basit soru algılandı, sadece ilk {len(cleaned_lines)} satır alındı")
                
                # Tekrar eden boş satırları temizle
                main_text = re.sub(r'\n{3,}', '\n\n', main_text)
                
                # Başlangıç ve son boşlukları temizle
                main_text = main_text.strip()
                
                # Eğer başlangıçta hala gereksiz bir başlık varsa kaldır
                if main_text.startswith("**"):
                    main_text = re.sub(r'^\*\*[^*]+\*\*\s*', '', main_text)
                
                # Başlangıç ve son boşlukları tekrar temizle
                main_text = main_text.strip()
                
                # KAYNAKLAR artık cevaba eklenmeyecek (ayrı olarak gösterilecek)
                return main_text
            
            # Cevabı temizle (Llama için)
            cleaned_response = clean_llama_response(ai_full_response.content, model_name, request.message)
            
            if "KAYNAKLAR:" in cleaned_response:
                parts = cleaned_response.split("KAYNAKLAR:", 1)
                ai_message_text = parts[0].strip()
                source_context_text = parts[1].strip().replace("- ", "").replace("* ", "")
                # Kaynak dosya adlarını parse et (virgül, satır başı veya başka ayırıcılardan)
                # Önce satır başına göre böl, sonra virgüle göre böl
                source_file_names = []
                for line in source_context_text.split('\n'):
                    # Her satırı kontrol et
                    line = line.strip()
                    if not line:
                        continue
                    # Virgülle ayrılmış ise böl
                    if ',' in line:
                        for item in line.split(','):
                            cleaned = item.strip()
                            if cleaned:
                                source_file_names.append(cleaned)
                    else:
                        # Virgül yoksa tüm satırı ekle
                        source_file_names.append(line)
                
                # Debug için
                print(f"DEBUG: Parse edilen kaynak dosyaları: {source_file_names}")
            else:
                ai_message_text = ai_full_response.content.strip()
                source_file_names = sorted(list({chunk.metadata['source_file_name'] for chunk in final_chunks if chunk.metadata.get('source_file_name')}))
                if source_file_names: source_context_text = ", ".join(source_file_names)
            
            # Token tracker'dan toplam token bilgisini al
            token_summary = token_tracker.get_summary()
            print("\n" + "="*70)
            print("📊 TOPLAM TOKEN KULLANIM ÖZETİ")
            print("="*70)
            print(f"   Toplam Giriş Token: {token_summary['total_input_tokens']:,}")
            print(f"   Toplam Çıkış Token: {token_summary['total_output_tokens']:,}")
            print(f"   TOPLAM TOKEN: {token_summary['total_tokens']:,}")
            print(f"   LLM Çağrı Sayısı: {token_summary['call_count']}")
            print(f"   Tahmini Maliyet (USD): ${token_summary['estimated_cost_usd']:.4f}")
            print(f"   Tahmini Maliyet (TL): {token_summary['estimated_cost_tl']:.2f} TL")
            print("="*70 + "\n")
            
            # Response için token usage bilgisini token tracker'dan al
            token_usage = {
                "input_tokens": token_summary['total_input_tokens'],
                "output_tokens": token_summary['total_output_tokens'],
                "total_tokens": token_summary['total_tokens'],
                "breakdown": token_summary['breakdown'],
                "estimated_cost_usd": token_summary['estimated_cost_usd'],
                "estimated_cost_tl": token_summary['estimated_cost_tl']
            }

            # Token bilgilerini cevabın altına ekle (kullanıcıya gösterilecek format)
            # token_info_text = ... (Kaldırıldı)
            # KAYNAKLAR bilgisini cevap metninden kaldır - sadece source_context'te gösterilecek
            # Cevaba sadece token bilgilerini ekle
            final_response_message = ai_message_text
            
            response_data = {
                "response_message": final_response_message, 
                "source_context": source_context_text or "Genel Bilgi", 
                "token_usage": token_usage,
                "source_file_names": source_file_names  # Bağlama eklemek için sakla
            }
    
    except Exception as e:
        print(f"LangChain RAG zinciri hatası: {e}\n{tb_module.format_exc()}")
        
        # Rate limit hatası için özel mesaj
        error_str = str(e)
        if "rate_limit" in error_str.lower() or "429" in error_str or "RateLimitError" in str(type(e)):
            error_message = "Yapay zeka servisi şu anda çok yoğun. Lütfen birkaç saniye bekleyip tekrar deneyin. Alternatif olarak, daha az veri içeren bir soru sorabilir veya başka bir AI modeli seçebilirsiniz."
        else:
            error_message = f"Yapay zeka modelinden yanıt alınırken bir hata oluştu: {str(e)[:200]}"
        
        # Hata durumunda bile token tracker'dan bilgi al
        if 'token_tracker' in locals():
            token_summary = token_tracker.get_summary()
            if token_summary['total_tokens'] > 0:
                print(f"⚠️ Hata oluştu, ancak hata öncesi {token_summary['total_tokens']:,} token kullanıldı.")
            # Hata durumunda da token bilgilerini ekle
            
            response_data = {
                "response_message": error_message, 
                "source_context": "Hata",
                "token_usage": {
                    "input_tokens": token_summary['total_input_tokens'],
                    "output_tokens": token_summary['total_output_tokens'],
                    "total_tokens": token_summary['total_tokens'],
                    "breakdown": token_summary['breakdown'],
                    "estimated_cost_usd": token_summary['estimated_cost_usd'],
                    "estimated_cost_tl": token_summary['estimated_cost_tl']
                },
                "source_file_names": []
            }
        else:
            response_data = {
                "response_message": error_message, 
                "source_context": "Hata", 
                "token_usage": {},
                "source_file_names": []
            }

    # --- DÜZELTME: METADATA HESAPLAMASINI DB KAYDINDAN ÖNCE YAP ---
    
    processing_time = time.monotonic() - t_start
    
    # Token kullanım özeti (varsa)
    token_summary_final = token_tracker.get_summary() if 'token_tracker' in locals() else None
    token_usage_for_metadata = response_data.get("token_usage", {})
    
    # Eğer token_usage boşsa ama token_tracker varsa, ondan al
    if not token_usage_for_metadata and token_summary_final:
        token_usage_for_metadata = {
            "input_tokens": token_summary_final['total_input_tokens'],
            "output_tokens": token_summary_final['total_output_tokens'],
            "total_tokens": token_summary_final['total_tokens'],
            "breakdown": token_summary_final['breakdown'],
            "estimated_cost_usd": token_summary_final['estimated_cost_usd'],
            "estimated_cost_tl": token_summary_final['estimated_cost_tl']
        }
    
    response_metadata = {
        "processing_time": round(processing_time, 2),
        "token_usage": token_usage_for_metadata,
        "source_context": response_data.get("source_context")
    }
    
    # --- KAYNAKLARI BAĞLAMA EKLE ---
    new_context_items = []
    source_file_names = response_data.get("source_file_names", []) if 'response_data' in locals() else []
    if source_file_names and 'response_data' in locals() and response_data.get("response_message"):
        try:
            # Dosya adlarından dosya bilgilerini bul
            all_tenant_files = get_all_accessible_files_for_user(db, user)
            matched_files = []
            seen_file_ids = set()  # Duplicate kontrolü için
            
            for file_name in source_file_names:
                # Dosya adını normalize et (tırnak işaretleri, boşluklar vb. temizle)
                original_file_name = file_name.strip().strip('"').strip("'")
                clean_file_name = original_file_name
                
                # Path'ten sadece dosya adını çıkar (örn: "satınalma/mail trafiği/dosya.docx" -> "dosya.docx")
                path_parts = []
                if "/" in clean_file_name:
                    path_parts = clean_file_name.split("/")
                    clean_file_name = path_parts[-1]
                elif "\\" in clean_file_name:
                    path_parts = clean_file_name.split("\\")
                    clean_file_name = path_parts[-1]
                
                # Normalize: Baştaki/sondaki boşlukları temizle
                clean_file_name = clean_file_name.strip()
                
                # Tam eşleşme öncelikli (dosya adı)
                matched = False
                for file in all_tenant_files:
                    if file.id in seen_file_ids:
                        continue  # Zaten eklenmiş, atla
                    
                    if file.name == clean_file_name:
                        matched_files.append(file)
                        seen_file_ids.add(file.id)
                        matched = True
                        print(f"✅ Tam eşleşme bulundu: '{clean_file_name}' -> '{file.name}'")
                        break
                
                # Tam eşleşme yoksa, path'li dosya adı ile eşleşme ara
                if not matched and path_parts:
                    original_path = original_file_name.replace("\\", "/")
                    file_name_from_path = path_parts[-1].strip() if path_parts else clean_file_name
                    path_folders = path_parts[:-1] if len(path_parts) > 1 else []
                    
                    for file in all_tenant_files:
                        if file.id in seen_file_ids:
                            continue
                        
                        # Dosya adı eşleşiyorsa (tam veya kısmi)
                        file_name_matches = (file.name == file_name_from_path or 
                                           file.name.lower() == file_name_from_path.lower() or
                                           file.name.endswith(file_name_from_path) or
                                           file_name_from_path in file.name)
                        
                        if file_name_matches:
                            # Path klasörleri varsa, dosyanın klasörünü kontrol et
                            if path_folders and file.folder_id:
                                folder = next((f for f in db.get_all_folders_for_tenant(user.tenant_id) if f.id == file.folder_id), None)
                                if folder:
                                    folder_name_lower = folder.name.lower()
                                    # Path'teki klasör isimlerinden biri dosyanın klasöründe geçiyor mu?
                                    path_match = any(part.lower() in folder_name_lower or folder_name_lower in part.lower() 
                                                   for part in path_folders)
                                    if path_match:
                                        matched_files.append(file)
                                        seen_file_ids.add(file.id)
                                        matched = True
                                        print(f"✅ Path ile eşleşme bulundu: '{original_path}' -> '{file.name}' (Klasör: {folder.name})")
                                        break
                            
                            # Path kontrolü yapılamazsa veya path yoksa, sadece dosya adı eşleşmesi yeterli
                            if not matched:
                                matched_files.append(file)
                                seen_file_ids.add(file.id)
                                matched = True
                                print(f"✅ Dosya adı eşleşmesi bulundu: '{file_name_from_path}' -> '{file.name}'")
                                break
                
                # Hala eşleşme yoksa, kısmi eşleşme dene (son çare)
                if not matched:
                    # Dosya adının büyük bir kısmı eşleşiyorsa kabul et
                    # Özellikle teklif, fatura, sözleşme gibi belgeler için
                    clean_lower = clean_file_name.lower()
                    if len(clean_file_name) > 10:  # Yeterince uzun dosya adları için
                        for file in all_tenant_files:
                            if file.id in seen_file_ids:
                                continue
                            
                            file_lower = file.name.lower()
                            # Kısmi eşleşme kontrolü: dosya adının önemli kısmı eşleşiyor mu?
                            # Örnek: "E-Posta Senaryosu SBYS Natural Rubber Teklifi" ile "E-Posta Senaryosu SBYS Natural Rubber TTeklifi" eşleşmeli
                            key_parts = [part.strip() for part in clean_lower.replace(".docx", "").replace(".pdf", "").split() 
                                       if len(part.strip()) > 3]  # 3 karakterden uzun kelimeler
                            
                            if key_parts:
                                # Dosya adında bu kelimelerin çoğu geçiyor mu?
                                matching_parts = sum(1 for part in key_parts if part in file_lower)
                                match_ratio = matching_parts / len(key_parts) if key_parts else 0
                                
                                # %70 veya daha fazla kelime eşleşiyorsa veya teklif/fatura gibi keyword varsa
                                has_keyword = any(kw in clean_lower for kw in ['teklif', 'fatura', 'invoice', 'sözleşme', 'agreement', 'contract', 'purchase', 'order'])
                                if match_ratio >= 0.7 or (has_keyword and match_ratio >= 0.5):
                                    matched_files.append(file)
                                    seen_file_ids.add(file.id)
                                    matched = True
                                    print(f"✅ Kısmi eşleşme (son çare): '{clean_file_name}' -> '{file.name}' (Eşleşme: %{match_ratio*100:.0f})")
                                    break
                
                if not matched:
                    print(f"⚠️ Kaynak dosya bulunamadı: '{original_file_name}' (temizlenmiş: '{clean_file_name}')")
            
            if matched_files:
                print(f"🔍 Kaynaklardan {len(matched_files)} dosya bulundu, bağlama ekleniyor...")
                
                # ALGILAMA: Kullanıcının sorusuna göre sadece alakalı dosyaları filtrele
                query_lower = request.message.lower()
                relevant_keywords = []
                
                # Sözleşme/agreement soruları için
                if any(word in query_lower for word in ['sözleşme', 'sözleşmem', 'agreement', 'contract']):
                    relevant_keywords.extend(['sözleşme', 'agreement', 'contract', 'satınalma', 'purchase', 'tedarikçi', 'supplier', 'vendor'])
                
                # Fatura/invoice soruları için
                if any(word in query_lower for word in ['fatura', 'invoice', 'ödeme', 'payment']):
                    relevant_keywords.extend(['fatura', 'invoice', 'payment', 'ödeme'])
                
                # Teklif soruları için
                if any(word in query_lower for word in ['teklif', 'quote', 'proposal', 'offer']):
                    relevant_keywords.extend(['teklif', 'quote', 'proposal', 'offer'])
                
                # Purchase Order soruları için
                if any(word in query_lower for word in ['satın alma', 'purchase', 'order', 'po', 'sipariş']):
                    relevant_keywords.extend(['purchase', 'order', 'po', 'satın alma', 'sipariş', 'satınalma'])
                
                # Eğer alakalı keyword varsa, sadece alakalı dosyaları filtrele
                filtered_files = matched_files
                if relevant_keywords:
                    filtered_files = []
                    for file in matched_files:
                        file_name_lower = file.name.lower()
                        # Dosya adında veya klasör adında alakalı kelime var mı?
                        if any(keyword in file_name_lower for keyword in relevant_keywords):
                            filtered_files.append(file)
                        elif file.folder_id:
                            folder = next((f for f in db.get_all_folders_for_tenant(user.tenant_id) if f.id == file.folder_id), None)
                            if folder and any(keyword in folder.name.lower() for keyword in relevant_keywords):
                                filtered_files.append(file)
                    
                    if filtered_files:
                        print(f"✅ {len(filtered_files)} alakalı dosya filtrelendi ({len(matched_files)} toplam)")
                    else:
                        # Filtre çok katı oldu, tüm dosyaları kullan
                        filtered_files = matched_files
                        print(f"⚠️ Filtre sonrası dosya kalmadı, tüm {len(matched_files)} dosya kullanılacak")
                else:
                    print(f"ℹ️ Alakalı keyword bulunamadı, tüm {len(matched_files)} dosya kullanılacak")
                
                # Dosyaların folder_id'lerini topla (filtrelenmiş dosyalardan)
                folder_ids = {file.folder_id for file in filtered_files if file.folder_id}
                
                if len(filtered_files) == 1:
                    # Tek dosya varsa, o dosyayı ekle
                    file = filtered_files[0]
                    new_context_items.append(ActiveContextFile(
                        id=file.id,
                        name=file.name,
                        type="file"
                    ))
                    print(f"✅ Tek dosya bağlama eklendi: {file.name}")
                elif len(folder_ids) == 1:
                    # Tüm dosyalar aynı klasördeyse, klasörü ekle
                    folder_id = list(folder_ids)[0]
                    folder_found = False
                    for f in db.get_all_folders_for_tenant(user.tenant_id):
                        if f.id == folder_id:
                            new_context_items.append(ActiveContextFile(
                                id=f.id,
                                name=f.name,
                                type="folder"
                            ))
                            print(f"✅ Tek klasör bağlama eklendi: {f.name}")
                            folder_found = True
                            break
                    if not folder_found:
                        # Klasör bulunamazsa dosyaları ekle
                        for file in filtered_files:
                            new_context_items.append(ActiveContextFile(
                                id=file.id,
                                name=file.name,
                                type="file"
                            ))
                        print(f"⚠️ Klasör bulunamadı, {len(filtered_files)} dosya eklendi")
                elif len(filtered_files) <= 5:
                    # 5 veya daha az dosya varsa, dosyaları ekle (klasör değil)
                    for file in filtered_files:
                        new_context_items.append(ActiveContextFile(
                            id=file.id,
                            name=file.name,
                            type="file"
                        ))
                    print(f"✅ {len(filtered_files)} dosya bağlama eklendi (az dosya olduğu için klasör yerine)")
                else:
                    # Çok fazla dosya varsa, en yaygın 3-5 klasörü ekle (tüm klasörleri değil!)
                    folder_counts = {}
                    for file in filtered_files:
                        if file.folder_id:
                            folder_counts[file.folder_id] = folder_counts.get(file.folder_id, 0) + 1
                    
                    # En çok dosya içeren klasörleri sırala
                    sorted_folders = sorted(folder_counts.items(), key=lambda x: x[1], reverse=True)
                    # En fazla 3-5 klasör ekle (veya dosyaların %80'ini kapsayan klasörler)
                    max_folders = min(5, len(sorted_folders))
                    total_files = len(filtered_files)
                    threshold = total_files * 0.8
                    
                    folders_added = set()
                    files_covered = 0
                    for folder_id, count in sorted_folders[:max_folders]:
                        if files_covered < threshold:
                            folder = next((f for f in db.get_all_folders_for_tenant(user.tenant_id) if f.id == folder_id), None)
                            if folder and folder_id not in folders_added:
                                new_context_items.append(ActiveContextFile(
                                    id=folder.id,
                                    name=folder.name,
                                    type="folder"
                                ))
                                folders_added.add(folder_id)
                                files_covered += count
                                print(f"✅ Klasör bağlama eklendi: {folder.name} ({count} dosya)")
                    
                    # Eğer klasör bulunamadıysa veya çok az dosya kapsandıysa, en önemli dosyaları ekle
                    if not folders_added or files_covered < total_files * 0.5:
                        # En çok geçen dosya isimlerine sahip dosyaları ekle
                        important_files = filtered_files[:min(10, len(filtered_files))]
                        for file in important_files:
                            new_context_items.append(ActiveContextFile(
                                id=file.id,
                                name=file.name,
                                type="file"
                            ))
                        print(f"⚠️ Klasör yeterli değil, {len(important_files)} önemli dosya eklendi")
                
                # Yeni context item'ları mevcut bağlama ekle
                if new_context_items:
                    existing_context = context_memory.get_context()
                    existing_ids = {item.id for item in existing_context}
                    # Sadece yeni olanları ekle (duplicate kontrolü)
                    for item in new_context_items:
                        if item.id not in existing_ids:
                            existing_context.append(item)
                    context_memory.set_context(existing_context)
                    print(f"📎 Toplam {len(existing_context)} kalem bağlama eklendi.")
        except Exception as e:
            print(f"⚠️ Kaynakları bağlama eklerken hata: {e}\n{tb_module.format_exc()}")
    
    # --- DÜZELTME: DB'YE MESAJI METADATA İLE KAYDET ---
    db.save_chat_message(
        chat_id, 
        user.tenant_id, 
        ChatMessage(
            sender="ai", 
            text=response_data["response_message"], 
            metadata=response_metadata
        )
    )
    
    final_active_context = context_memory.get_context()

    return ChatResponse(
        response_message=response_data["response_message"], source_context=response_data.get("source_context"),
        chat_id=chat_id, active_context_files=final_active_context,
        response_type="answer", suggested_file=None,
        response_metadata=response_metadata
    )