# backend/app/services/chat_context.py
import logging
import traceback
from collections import Counter, defaultdict
from typing import List, Dict, Set, Optional
from app.schemas.file import FileOut # <-- YENİ IMPORT
from app.schemas.chat import ActiveContextFile
from app.schemas.user import UserInDB
from app.repositories.base import BaseRepository
from langchain_core.documents import Document # <-- Bu import'un burada olduğundan emin olun

logger = logging.getLogger(__name__)

class ContextMemory:
    """Sohbet oturumu başına aktif bağlamı (dosyalar/klasörler) yönetir."""
    def __init__(self): 
        self.context_items: Dict[str, ActiveContextFile] = {}
    
    def set_context(self, items: List[ActiveContextFile]):
        """Bağlamı ayarlar."""
        self.context_items = {item.id: item for item in items}
        logger.info(f"🧠 Bağlam Hafızası Ayarlandı: {len(self.context_items)} kalem.")
    
    def get_context(self) -> List[ActiveContextFile]: 
        """Mevcut bağlam listesini döndürür."""
        return list(self.context_items.values())
    
    def get_file_ids(self) -> Set[str]: 
        """Bağlamdaki dosya ID'lerini döndürür."""
        return {item.id for item in self.context_items.values() if item.type == 'file'}
    
    def get_folder_ids(self) -> Set[str]: 
        """Bağlamdaki klasör ID'lerini döndürür."""
        return {item.id for item in self.context_items.values() if item.type == 'folder'}
    
    def has_context(self) -> bool: 
        """Bağlamda öğe olup olmadığını kontrol eder."""
        return bool(self.context_items)
    
    def clear(self):
        """Bağlamı temizler."""
        self.context_items = {}
        logger.info("🗑️ Bağlam Hafzası Temizlendi")

# Her chat_id için bir ContextMemory örneği tutan global depo
_context_memory_store: Dict[str, ContextMemory] = {}

def get_context_memory_for_chat(chat_id: str) -> ContextMemory:
    """Belirli bir sohbet ID'si için ContextMemory örneğini alır veya oluşturur."""
    if chat_id not in _context_memory_store:
        _context_memory_store[chat_id] = ContextMemory()
    return _context_memory_store[chat_id]

def resolve_context_file_ids(
    context_memory: ContextMemory, 
    db: BaseRepository, 
    user: UserInDB
) -> Set[str]:
    """
    ContextMemory'deki dosya ve klasör ID'lerinden yola çıkarak
    aranacak tüm dosya ID'lerini bulanıklaştırır.
    """
    search_file_ids = context_memory.get_file_ids()
    search_folder_ids = context_memory.get_folder_ids()
    
    if search_folder_ids:
        for folder_id in search_folder_ids:
            try:
                file_ids_in_folder = db.get_all_file_ids_in_folder_recursive(
                    tenant_id=user.tenant_id, folder_id=folder_id, user=user
                )
                search_file_ids.update(file_ids_in_folder)
            except Exception as e:
                logger.error(f"Klasör içeriği alınırken hata: {e}")
    
    return search_file_ids
    
def get_all_accessible_files_for_user(db: BaseRepository, user: UserInDB) -> List[FileOut]:
    """
    Bir kullanıcının (Admin değilse) erişebileceği TÜM dosya objelerini (FileOut) döndürür.
    Buna sahip olduğu, doğrudan izin verilen veya izin verilen bir klasörde bulunan dosyalar dahildir.
    """
    if user.role == "Admin":
        logger.info(f"Kullanıcı '{user.email}' Admin. Tüm tenant dosyaları getiriliyor.")
        return db.get_all_files_for_tenant(tenant_id=user.tenant_id)

    logger.info(f"Kullanıcı '{user.email}' (Rol: {user.role}) için erişilebilir dosyalar hesaplanıyor...")
    
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
    
    logger.info(f"Kullanıcı {len(accessible_files)} adet dosyaya erişebilir.")
    return accessible_files
# --- YENİ FONKSİYON BİTTİ ---

def update_context_automatically(
    context_memory: ContextMemory,
    # --- DEĞİŞİKLİK: Parametre adını daha genel hale getiriyoruz ---
    chunks_to_analyze: List[Document], # 'final_chunks' yerine
    # --- DEĞİŞİKLİK BİTTİ ---
    db: BaseRepository,
    user: UserInDB,
    is_general_search: bool,
    user_explicitly_cleared_context: bool,
    response_message: Optional[str]
) -> List[ActiveContextFile]:
    """
    Genel bir arama yapıldıysa ve cevap bulunduysa,
    cevabın kaynağı olan dosya/klasörü otomatik olarak bağlama ekler.
    Analiz için 'chunks_to_analyze' listesini kullanır.
    """
    final_active_context = context_memory.get_context()

    # --- DEĞİŞİKLİK: 'final_chunks' yerine 'chunks_to_analyze' kullanın ---
    if not (is_general_search and chunks_to_analyze and response_message and not user_explicitly_cleared_context):
    # --- DEĞİŞİKLİK BİTTİ ---
        # Otomatik bağlam ekleme koşulları sağlanmadı
        return final_active_context

    logger.info("Genel arama bitti, cevabı içeren kaynak dosya/klasör otomatik olarak bağlama ekleniyor...")
    print("✅ Otomatik bağlam ekleme koşulları sağlandı, dosya/klasör aranıyor...")

    # 1. chunks_to_analyze içindeki tüm farklı dosya ID'lerini topla
    unique_file_ids = set()
    file_id_to_name = {}
    # --- DEĞİŞİKLİK: 'final_chunks' yerine 'chunks_to_analyze' kullanın ---
    for chunk in chunks_to_analyze:
    # --- DEĞİŞİKLİK BİTTİ ---
        file_id = chunk.metadata.get("source_file_id")
        file_name = chunk.metadata.get("source_file_name")
        if file_id and file_name:
            unique_file_ids.add(file_id)
            file_id_to_name[file_id] = file_name
    
    print(f"📊 Cevap için kullanılan (analiz edilen) dosya sayısı: {len(unique_file_ids)}")
    
    # 2. Dosya bilgilerini veritabanından al (folder_id için)
    new_context_items: List[ActiveContextFile] = []
    
    if len(unique_file_ids) == 1:
        # Tek dosya varsa → O dosyayı bağlama ekle
        single_file_id = list(unique_file_ids)[0]
        single_file_name = file_id_to_name.get(single_file_id, "Bilinmeyen Dosya")
        new_context_items.append(ActiveContextFile(id=single_file_id, name=single_file_name, type='file'))
        logger.info(f"Bağlama eklenecek tek dosya: '{single_file_name}'")
        print(f"✅ Otomatik bağlam eklendi (tek dosya): '{single_file_name}' (ID: {single_file_id})")
    
    elif len(unique_file_ids) > 1:
        # Birden fazla dosya varsa → En alakalı klasörü bul ve bağlama ekle
        try:
            all_files = []
            for file_id in unique_file_ids:
                file_record = db.get_file_by_id(user.tenant_id, file_id)
                if file_record:
                    all_files.append(file_record)
            
            print(f"📁 {len(all_files)} dosya bilgisi alındı (toplam {len(unique_file_ids)} ID)")
            
            unique_folder_ids = {f.folder_id for f in all_files if f.folder_id}
            
            if unique_folder_ids:
                all_tenant_folders = db.get_all_folders_for_tenant(user.tenant_id)
                candidate_folders = [f for f in all_tenant_folders if f.id in unique_folder_ids]
                
                folder_stats = {}
                file_id_to_chunks = defaultdict(list)
                # --- DEĞİŞİKLİK ---
                for chunk in chunks_to_analyze:
                # --- DEĞİŞİKLİK BİTTİ ---
                    if file_id := chunk.metadata.get("source_file_id"):
                        file_id_to_chunks[file_id].append(chunk)
                
                for folder in candidate_folders:
                    files_in_folder = [f for f in all_files if f.folder_id == folder.id]
                    total_score, chunk_count = 0.0, 0
                    for file_record in files_in_folder:
                        for chunk in file_id_to_chunks.get(file_record.id, []):
                            total_score += chunk.metadata.get('hybrid_score', 0.0)
                            chunk_count += 1
                    
                    folder_stats[folder.id] = {
                        'folder': folder, 'file_count': len(files_in_folder),
                        'avg_score': total_score / chunk_count if chunk_count else 0.0
                    }
                
                # --- DEĞİŞİKLİK ---
                file_paths = [c.metadata.get('source_file_name', '') for c in chunks_to_analyze]
                # --- DEĞİŞİKLİK BİTTİ ---
                common_prefix_folder = None
                path_prefixes = [p.split('/')[0] for p in file_paths if '/' in p]
                
                if path_prefixes:
                    prefix_counts = Counter(path_prefixes)
                    most_common = prefix_counts.most_common(1)[0] if prefix_counts else None
                    if most_common and most_common[1] >= len(path_prefixes) * 0.4:
                        for folder in candidate_folders:
                            if folder.name.lower() == most_common[0].lower():
                                common_prefix_folder = folder
                                break
                
                best_folder, best_score = None, -1
                for folder_id, stats in folder_stats.items():
                    score = (stats['file_count'] * 10) + (stats['avg_score'] * 100)
                    if common_prefix_folder and stats['folder'].id == common_prefix_folder.id:
                        score += 200
                    
                    if score > best_score:
                        best_score, best_folder = score, stats['folder']
                
                if best_folder:
                    new_context_items.append(ActiveContextFile(id=best_folder.id, name=best_folder.name, type='folder'))
                    stats = folder_stats.get(best_folder.id, {})
                    logger.info(f"Bağlama eklenecek en iyi klasör: '{best_folder.name}' (skor: {best_score:.1f}, dosya sayısı: {stats.get('file_count', 0)}, ort. skor: {stats.get('avg_score', 0.0):.3f})")
                    print(f"✅ Otomatik bağlam eklendi (en iyi klasör): '{best_folder.name}' (skor: {best_score:.1f})")
                elif candidate_folders:
                    fallback_folder = candidate_folders[0]
                    new_context_items.append(ActiveContextFile(id=fallback_folder.id, name=fallback_folder.name, type='folder'))
                    logger.info(f"Bağlama eklenecek klasör (fallback): '{fallback_folder.name}'")
                    print(f"✅ Otomatik bağlam eklendi (fallback): '{fallback_folder.name}'")
            else:
                logger.warning(f"{len(unique_file_ids)} dosya bulundu ama klasörleri tespit edilemedi.")
                print(f"⚠️ {len(unique_file_ids)} dosya bulundu ama tümü kök dizinde.")
        except Exception as e:
            logger.error(f"Klasör bilgileri alınırken hata: {e}\n{traceback.format_exc()}")
            print(f"⚠️ Klasör bilgileri alınırken hata: {e}")
    
    # 3. Bağlamı ayarla
    if new_context_items:
        context_memory.set_context(new_context_items)
        return new_context_items
    else:
        logger.info("Bağlama eklenecek geçerli bir kaynak dosya/klasör bulunamadı.")
        print("⚠️ Otomatik bağlam için geçerli dosya/klasör bulunamadı")
        return final_active_context