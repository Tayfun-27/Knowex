# backend/app/services/vector_service.py - OPTİMİZE EDİLDİ (Lazy Loading)
# LocalGPT tarzı Hibrit Arama ve Reranking desteği eklendi

import re
from typing import List, Set, Optional, Dict, Tuple
from functools import lru_cache  # <-- ÖNEMLİ: Bu eklendi
from collections import defaultdict

from app.schemas.file import FileOut
from app.schemas.user import UserInDB
from app.repositories.base import BaseRepository
from app.storage_adapters.base import BaseStorageAdapter
from app.core import parsers, chunker
from app.core.config import GEMINI_API_KEY

# --- LangChain Importları ---
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_google_firestore import FirestoreVectorStore
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

# --- BM25 için ---
try:
    from rank_bm25 import BM25Okapi
    BM25_AVAILABLE = True
except ImportError:
    BM25_AVAILABLE = False
    print("⚠️ rank-bm25 paketi yüklü değil. BM25 araması devre dışı.")

# --- Firestore FieldFilter ---
from google.cloud.firestore_v1.base_query import FieldFilter

# --- KRİTİK DEĞİŞİKLİK: Global değişkeni SİLDİK ---
# embedding_model = GoogleGenerativeAIEmbeddings(...) # BU SATIR ARTIK YOK

# --- YERİNE BU FONKSİYONU EKLEDİK ---
@lru_cache(maxsize=1)
def get_embedding_model():
    """
    Embedding modelini uygulama açılırken değil,
    İLK KEZ İHTİYAÇ DUYULDUĞUNDA yükler.
    """
    print("⚡ Embedding modeli ilk kez yükleniyor (Lazy Load)...")
    try:
        return GoogleGenerativeAIEmbeddings(
            model="models/text-embedding-004", 
            google_api_key=GEMINI_API_KEY
        )
    except Exception as e:
        print(f"Google Generative AI Embeddings yapılandırılamadı: {e}")
        return None

def index_file(
    file_record: FileOut,
    user: UserInDB,
    db: BaseRepository,
    storage: BaseStorageAdapter
):
    """Dosyayı vektörleştirip kaydeder."""
    print(f"Indexing işlemi başladı: {file_record.name}")

    try:
        # DEĞİŞİKLİK: Global değişken yerine fonksiyonu çağırıyoruz
        embedding_model = get_embedding_model() # <-- BURASI DEĞİŞTİ

        if not embedding_model:
            raise Exception("Embedding modeli başlatılamadığı için indexleme yapılamıyor.")

        # Eğer dosya external storage'dan geliyorsa, Google Drive/OneDrive'dan geçici olarak indir
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
                        print(f"⚠️ Google Drive bağlantısı bulunamadı, indexleme atlanıyor: {file_record.name}")
                        return
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
                        print(f"⚠️ OneDrive bağlantısı bulunamadı, indexleme atlanıyor: {file_record.name}")
                        return
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
                print(f"⚠️ Desteklenmeyen storage tipi: {storage_type}, indexleme atlanıyor: {file_record.name}")
                return
            
            if not access_token:
                print(f"⚠️ Access token bulunamadı, indexleme atlanıyor: {file_record.name}")
                return
            
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
                        print(f"⚠️ Token yenileme başarısız, indexleme atlanıyor ({file_record.name}): {refresh_error}")
                        return
                else:
                    print(f"⚠️ Dosya indirilemedi, indexleme atlanıyor ({file_record.name}): {e}")
                    return
        else:
            # Normal dosyalar için mevcut mantık
            if not file_record.storage_path:
                print(f"⚠️ Storage path bulunamadı, indexleme atlanıyor: {file_record.name}")
                return
            file_bytes = storage.download_file_content(storage_path=file_record.storage_path)
        
        text_content = parsers.extract_text_from_file(
            file_bytes=file_bytes,
            file_name=file_record.name,
            mime_type=file_record.content_type
        )

        if not text_content or text_content.strip().startswith("["):
            print(f"İçerik okunamadı: {file_record.name}")
            return

        metadata_prefix = ""
        match = re.search(r"(?:To|Alıcı)\s*:\s*(.*)", text_content, re.IGNORECASE)
        if match:
            customer_name = match.group(1).strip().split('\n')[0].strip()
            if customer_name:
                metadata_prefix = f"Bu doküman, '{customer_name}' müşterisine aittir.\n"

        chunks = chunker.get_text_chunks(text_content, chunk_size=1000, overlap=150)
        batch_size = 5
        chunk_batch = []

        for i, chunk_text in enumerate(chunks):
            enriched_chunk_text = metadata_prefix + chunk_text
            # Embedding oluştur
            embedding = embedding_model.embed_documents([enriched_chunk_text])[0]

            if embedding:
                chunk_data = {
                    "tenant_id": user.tenant_id,
                    "file_id": file_record.id,
                    "file_name": file_record.name,
                    "chunk_number": i,
                    "chunk_text": enriched_chunk_text,
                    "embedding": embedding
                }
                chunk_batch.append(chunk_data)

            if len(chunk_batch) >= batch_size:
                db.add_text_chunks_batch(chunk_batch)
                chunk_batch = []

        if chunk_batch:
            db.add_text_chunks_batch(chunk_batch)

        print(f"Indexing tamamlandı: {file_record.name}")

    except Exception as e:
        print(f"Dosya indexlenirken hata: {e}")


def search_similar_chunks(
    tenant_id: str,
    query: str,
    db: BaseRepository,
    limit: int = 15,
    filter_file_ids: Optional[Set[str]] = None
) -> List[dict]:
    """Kullanıcı sorusuna benzer metinleri arar."""
    try:
        # DEĞİŞİKLİK: Global değişken yerine fonksiyonu çağırıyoruz
        embedding_model = get_embedding_model() # <-- BURASI DEĞİŞTİ
        
        if not embedding_model:
            raise Exception("Embedding modeli başlatılamadı.")
            
        query_embedding = embedding_model.embed_query(query)
        
        if not query_embedding:
            raise Exception("Sorgu vektörü oluşturulamadı.")

        similar_chunks = db.find_similar_chunks(
            tenant_id=tenant_id,
            query_vector=query_embedding,
            limit=limit,
            file_id_filter=filter_file_ids
        )

        results = [
            {
                "id": chunk.get("id"),
                "text": chunk.get("chunk_text"),
                "source_file_name": chunk.get("file_name"),
                "source_file_id": chunk.get("file_id"),
                "similarity_score": chunk.get("similarity_score", 0.0)
            }
            for chunk in similar_chunks
        ]
        return results

    except Exception as e:
        print(f"Vektör araması hatası: {e}")
        return []


# --- BM25 Index Cache (tenant bazlı) ---
_bm25_index_cache: Dict[str, Tuple] = {}  # {cache_key: (bm25_index, chunk_list)}


def _tokenize_turkish(text: str) -> List[str]:
    """Türkçe metni tokenize et (basit yaklaşım)."""
    # Türkçe karakterleri koru, küçük harfe çevir, kelimelere böl
    text = text.lower()
    # Noktalama işaretlerini kaldır, kelimelere böl
    words = re.findall(r'\b\w+\b', text)
    return words


def build_bm25_index(
    tenant_id: str,
    db: BaseRepository,
    filter_file_ids: Optional[Set[str]] = None
) -> Tuple:
    """BM25 index oluştur (cache'lenir)."""
    if not BM25_AVAILABLE:
        return None, []
    
    # Cache key oluştur
    filter_key = hash(tuple(sorted(filter_file_ids or []))) if filter_file_ids else None
    cache_key = f"{tenant_id}_{filter_key}"
    
    if cache_key in _bm25_index_cache:
        print(f"📦 BM25 index cache'den yüklendi (tenant: {tenant_id})")
        return _bm25_index_cache[cache_key]
    
    print(f"🔨 BM25 index oluşturuluyor (tenant: {tenant_id})...")
    
    # Tüm chunk'ları al
    all_chunks = []
    try:
        # Firestore'dan chunk'ları çek
        if hasattr(db, 'db'):
            chunks_collection = db.db.collection("text_chunks")
            query = chunks_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))
            
            if filter_file_ids and len(filter_file_ids) <= 10:
                # Firestore 'in' operatörü limiti 10
                file_id_list = list(filter_file_ids)[:10]
                query = query.where(filter=FieldFilter("file_id", "in", file_id_list))
            elif filter_file_ids and len(filter_file_ids) > 10:
                # Çok fazla file_id varsa, tüm chunk'ları al ve sonra filtrele
                print(f"⚠️ {len(filter_file_ids)} file_id var, tüm chunk'lar alınıp sonra filtrelenecek")
            
            docs = query.stream()
            
            for doc in docs:
                doc_data = doc.to_dict()
                chunk_file_id = doc_data.get("file_id", "")
                
                # Eğer filter_file_ids varsa ve 10'dan fazlaysa, manuel filtrele
                if filter_file_ids and len(filter_file_ids) > 10:
                    if chunk_file_id not in filter_file_ids:
                        continue
                
                all_chunks.append({
                    "id": doc.id,
                    "text": doc_data.get("chunk_text", ""),
                    "file_id": chunk_file_id,
                    "file_name": doc_data.get("file_name", ""),
                })
        else:
            print("⚠️ Firestore repository'ye erişilemedi, BM25 index oluşturulamadı")
            return None, []
            
    except Exception as e:
        print(f"⚠️ BM25 index oluşturulurken hata: {e}")
        return None, []
    
    if not all_chunks:
        print("⚠️ BM25 index için chunk bulunamadı")
        return None, []
    
    # Tokenize et
    tokenized_corpus = [_tokenize_turkish(chunk["text"]) for chunk in all_chunks]
    
    # BM25 index oluştur
    try:
        bm25 = BM25Okapi(tokenized_corpus)
    except Exception as e:
        print(f"⚠️ BM25 index oluşturulamadı: {e}")
        return None, []
    
    # Cache'e kaydet
    _bm25_index_cache[cache_key] = (bm25, all_chunks)
    
    print(f"✅ BM25 index oluşturuldu: {len(all_chunks)} chunk")
    return bm25, all_chunks


def search_with_bm25(
    tenant_id: str,
    query: str,
    db: BaseRepository,
    limit: int = 15,
    filter_file_ids: Optional[Set[str]] = None
) -> List[dict]:
    """BM25 tabanlı keyword search yapar (LocalGPT tarzı)."""
    if not BM25_AVAILABLE:
        return []
    
    try:
        # BM25 index oluştur veya cache'den al
        bm25_index, chunk_list = build_bm25_index(tenant_id, db, filter_file_ids)
        
        if not bm25_index or not chunk_list:
            return []
        
        # Query'yi tokenize et
        tokenized_query = _tokenize_turkish(query)
        
        if not tokenized_query:
            return []
        
        # BM25 skorlarını hesapla
        scores = bm25_index.get_scores(tokenized_query)
        
        # Skorlu chunk'ları oluştur
        scored_chunks = []
        for i, (chunk, score) in enumerate(zip(chunk_list, scores)):
            if score > 0:  # Sadece pozitif skorlu chunk'ları al
                scored_chunks.append({
                    "id": chunk["id"],
                    "text": chunk["text"],
                    "source_file_name": chunk.get("file_name", ""),
                    "source_file_id": chunk.get("file_id", ""),
                    "bm25_score": float(score),
                    "rank": i + 1
                })
        
        # Skora göre sırala
        scored_chunks.sort(key=lambda x: x["bm25_score"], reverse=True)
        
        print(f"🔍 BM25 araması: {len(scored_chunks)} sonuç bulundu (query: '{query[:50]}...')")
        
        return scored_chunks[:limit]
        
    except Exception as e:
        print(f"❌ BM25 araması hatası: {e}")
        return []


def hybrid_search_similar_chunks(
    tenant_id: str,
    query: str,
    db: BaseRepository,
    limit: int = 15,
    filter_file_ids: Optional[Set[str]] = None,
    retrieval_mode: str = "hybrid"  # "hybrid", "vector", "bm25"
) -> List[dict]:
    """
    LocalGPT tarzı hibrit arama: Vector + BM25 + RRF.
    
    Args:
        retrieval_mode: "hybrid" (vector + bm25), "vector" (sadece semantic), "bm25" (sadece keyword)
    """
    try:
        if retrieval_mode == "vector":
            # Sadece semantic search
            return search_similar_chunks(tenant_id, query, db, limit, filter_file_ids)
        
        elif retrieval_mode == "bm25":
            # Sadece keyword search
            return search_with_bm25(tenant_id, query, db, limit, filter_file_ids)
        
        else:  # hybrid
            # 1. Vector search (semantic)
            vector_results = search_similar_chunks(
                tenant_id=tenant_id,
                query=query,
                db=db,
                limit=limit * 2,  # Daha fazla sonuç al
                filter_file_ids=filter_file_ids
            )
            
            # 2. BM25 search (keyword) - eğer mevcut değilse sadece vector kullan
            bm25_results = []
            if BM25_AVAILABLE:
                bm25_results = search_with_bm25(
                    tenant_id=tenant_id,
                    query=query,
                    db=db,
                    limit=limit * 2,
                    filter_file_ids=filter_file_ids
                )
            
            # Eğer BM25 sonuç yoksa, sadece vector sonuçlarını döndür
            if not bm25_results:
                print(f"⚠️ BM25 sonuç bulunamadı, sadece vector search kullanılıyor")
                return vector_results[:limit]
            
            # 3. Reciprocal Rank Fusion (RRF) - LocalGPT'in kullandığı yöntem
            # RRF_score = sum(1 / (k + rank)) for each retrieval method
            k = 60  # LocalGPT'in kullandığı değer
            
            combined_results = {}
            
            # Vector sonuçlarını ekle
            for rank, result in enumerate(vector_results, 1):
                chunk_id = result.get("id")
                if chunk_id not in combined_results:
                    combined_results[chunk_id] = {
                        **result,
                        "vector_rank": rank,
                        "vector_score": result.get("similarity_score", 0.0),
                        "bm25_rank": None,
                        "bm25_score": 0.0,
                        "rrf_score": 0.0
                    }
            
            # BM25 sonuçlarını ekle
            for rank, result in enumerate(bm25_results, 1):
                chunk_id = result.get("id")
                if chunk_id in combined_results:
                    combined_results[chunk_id]["bm25_rank"] = rank
                    combined_results[chunk_id]["bm25_score"] = result.get("bm25_score", 0.0)
                else:
                    combined_results[chunk_id] = {
                        **result,
                        "vector_rank": None,
                        "vector_score": 0.0,
                        "bm25_rank": rank,
                        "bm25_score": result.get("bm25_score", 0.0),
                        "rrf_score": 0.0
                    }
            
            # RRF skoru hesapla
            for chunk_id, result in combined_results.items():
                rrf_score = 0.0
                
                # Vector RRF
                if result.get("vector_rank"):
                    rrf_score += 1.0 / (k + result["vector_rank"])
                
                # BM25 RRF
                if result.get("bm25_rank"):
                    rrf_score += 1.0 / (k + result["bm25_rank"])
                
                result["rrf_score"] = rrf_score
                result["hybrid_score"] = rrf_score  # Aynı değer (uyumluluk için)
            
            # RRF score'a göre sırala
            final_results = sorted(
                combined_results.values(),
                key=lambda x: x.get("rrf_score", 0.0),
                reverse=True
            )
            
            print(f"🔀 Hibrit arama (RRF): {len(vector_results)} vector + {len(bm25_results)} BM25 = {len(final_results)} birleşik sonuç")
            
            return final_results[:limit]
            
    except Exception as e:
        print(f"❌ Hibrit arama hatası: {e}")
        # Fallback: Sadece semantic search
        return search_similar_chunks(tenant_id, query, db, limit, filter_file_ids)


def get_firestore_retriever(
    tenant_id: str,
    filter_file_ids: Optional[List[str]] = None
) -> BaseRetriever:
    """LangChain retriever oluşturur."""
    # DEĞİŞİKLİK: Global değişken yerine fonksiyonu çağırıyoruz
    embedding_model = get_embedding_model() # <-- BURASI DEĞİŞTİ

    if not embedding_model:
        raise Exception("Embedding modeli yüklenemedi.")

    vector_store = FirestoreVectorStore(
        collection="text_chunks",
        embedding_service=embedding_model,
    )

    search_kwargs = {'k': 150}
    if filter_file_ids:
         # print(f"Retriever filtrelendi...")
         pass

    return vector_store.as_retriever(search_kwargs=search_kwargs)
def warmup_model_in_background():
    """
    Bu fonksiyon sunucu açıldığında arka planda çalıştırılır.
    Amacı: get_embedding_model() fonksiyonunu bir kez çalıştırıp
    önbelleğe (cache) alınmasını sağlamaktır.
    """
    try:
        print("🔥 Arka plan model ısıtma işlemi başladı...")
        # Modeli çağırarak lru_cache'in dolmasını sağlıyoruz
        model = get_embedding_model()
        if model:
            print("✅ Model arka planda başarıyla yüklendi ve hazır!")
        else:
            print("⚠️ Model ısıtma sırasında yüklenemedi.")
    except Exception as e:
        print(f"⚠️ Model ısıtma hatası: {e}")