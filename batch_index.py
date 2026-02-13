# backend/batch_index.py
import os
import sys
import time

# Proje kök dizinini (backend/app) Python yoluna ekle
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'app')))

try:
    from app.repositories.firestore_repo import FirestoreRepository
    from app.storage_adapters.firebase_storage import FirebaseStorageAdapter
    from app.services import vector_service
    from app.schemas.user import UserInDB
    from app.core.config import GOOGLE_APPLICATION_CREDENTIALS
    from google.cloud.firestore_v1.base_query import FieldFilter
    
    # LÜTFEN BURAYI KONTROL EDİN:
    # create_admin.py'de kullandığınız admin e-posta adresi ne ise
    # buraya da onu yazın.
    ADMIN_EMAIL_FOR_TENANT_LOOKUP = "admin@acme.com" 

except ImportError as e:
    print(f"Hata: Gerekli modüller import edilemedi. {e}")
    print("Lütfen script'i 'backend/' dizininden çalıştırdığınızdan emin olun.")
    sys.exit(1)

if GOOGLE_APPLICATION_CREDENTIALS:
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GOOGLE_APPLICATION_CREDENTIALS
else:
    print("UYARI: GOOGLE_APPLICATION_CREDENTIALS ortam değişkeni ayarlanmadı.")
    sys.exit(1)


def check_if_file_indexed(db: FirestoreRepository, file_id: str, tenant_id: str) -> bool:
    """Dosyanın zaten 'text_chunks' içinde olup olmadığını kontrol eder."""
    try:
        # gcloud komutuyla (collection-group) eşleşmesi için 
        # doğrudan .collection_group() kullanıyoruz.
        query = db.db.collection_group('text_chunks').where(filter=FieldFilter("tenant_id", "==", tenant_id)) \
                                      .where(filter=FieldFilter("file_id", "==", file_id)) \
                                      .limit(1)
        results = list(query.stream())
        return len(results) > 0
    except Exception as e:
        print(f"  [Hata] İndeks kontrol edilemedi (dosya: {file_id}): {e}")
        return False # Hata olursa, yeniden indekslemeyi dene

def run_batch_indexing():
    print("Toplu Vektör İndeksleme Script'i Başlatıldı...")
    
    try:
        db = FirestoreRepository()
        storage = FirebaseStorageAdapter()
        
        # 1. Tenant ID'yi bulmak için admin kullanıcısını al
        print(f"'{ADMIN_EMAIL_FOR_TENANT_LOOKUP}' kullanıcısı aranıyor...")
        admin_user = db.get_user_by_email(ADMIN_EMAIL_FOR_TENANT_LOOKUP)
        
        if not admin_user:
            print(f"Hata: '{ADMIN_EMAIL_FOR_TENANT_LOOKUP}' kullanıcısı bulunamadı.")
            print("Lütfen 'ADMIN_EMAIL_FOR_TENANT_LOOKUP' değişkenini güncelleyin veya 'create_admin.py' script'ini çalıştırın.")
            return

        tenant_id = admin_user.tenant_id
        # UserInDB şemasına uyması için UserInDB'ye dönüştür
        user = UserInDB.model_validate(admin_user)
        print(f"Tenant ID bulundu: {tenant_id}. Bu tenant için tüm dosyalar getiriliyor...")

        # 2. Tenant'a ait tüm dosyaları al
        all_files = db.get_all_files_for_tenant(tenant_id=tenant_id)
        
        if not all_files:
            print("Bu tenant için 'files' koleksiyonunda hiç dosya bulunamadı.")
            return
            
        print(f"Toplam {len(all_files)} adet dosya bulundu. İndeksleme başlıyor...")
        print("--------------------------------------------------")

        indexed_count = 0
        skipped_count = 0

        # 3. Her dosyayı döngüye al ve indeksle
        for i, file_record in enumerate(all_files):
            print(f"[{i+1}/{len(all_files)}] Dosya işleniyor: {file_record.name} (ID: {file_record.id})")
            
            # 4. Zaten indekslenmiş mi diye kontrol et
            if check_if_file_indexed(db, file_record.id, tenant_id):
                print("  [Bilgi] Bu dosya zaten indekslenmiş. Atlanıyor.")
                skipped_count += 1
                continue
                
            # 5. İndeksle
            try:
                # Dosyayı indirir, okur, parçalar, vektör oluşturur ve 'text_chunks'a kaydeder.
                vector_service.index_file(file_record, user, db, storage)
                print(f"  [Başarılı] Dosya başarıyla indekslendi.")
                indexed_count += 1
                
                # API limitlerine takılmamak için kısa bir bekleme
                time.sleep(1) 
                
            except Exception as e:
                print(f"  [HATA] Dosya indekslenirken hata oluştu: {e}")
        
        print("--------------------------------------------------")
        print("Toplu İndeksleme Tamamlandı.")
        print(f"Başarılı: {indexed_count} dosya")
        print(f"Atlandı (Zaten vardı): {skipped_count} dosya")

    except Exception as e:
        print(f"Script çalışırken kritik bir hata oluştu: {e}")

if __name__ == "__main__":
    run_batch_indexing()