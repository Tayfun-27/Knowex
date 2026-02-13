# backend/create_admin.py
# (Multi-Tenant için güncellendi)

import os
import sys
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'app')))

try:
    from app.core.security import get_password_hash
    from app.schemas.user import UserInDB
    from app.core.config import GOOGLE_APPLICATION_CREDENTIALS
except ImportError as e:
    print(f"Hata: Gerekli modüller import edilemedi. {e}")
    sys.exit(1)

# --- Burayı Düzenleyin --- 
TENANT_NAME = "Delta Hidrolik"        # Oluşturulacak ilk firmanızın adı
ADMIN_EMAIL = "i.seymen@deltahidrolik.com"# Admin email
ADMIN_PASSWORD = "D@ltaHidrolik2026" 
ADMIN_FULL_NAME = "İsmail Seymen"
# -------------------------

if GOOGLE_APPLICATION_CREDENTIALS:
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GOOGLE_APPLICATION_CREDENTIALS
else:
    print("UYARI: GOOGLE_APPLICATION_CREDENTIALS ortam değişkeni ayarlanmadı.")
    print("Lütfen 'service-account.json' dosyanızın yolunu ayarladığınızdan emin olun.")
    # Script yine de devam etmeyi deneyebilir (eğer ortamda varsayılan auth varsa)

def create_admin_and_tenant():
    try:
        db = firestore.Client()
        tenants_collection = db.collection('tenants')
        users_collection = db.collection('users')
        
        # 1. İlk Tenant'ı (Firma) Oluştur
        print(f"'{TENANT_NAME}' adında yeni bir tenant oluşturuluyor...")
        tenant_data = {"name": TENANT_NAME, "status": "active"}
        tenant_ref = tenants_collection.document()
        tenant_ref.set(tenant_data)
        new_tenant_id = tenant_ref.id
        print(f"Tenant başarıyla oluşturuldu. Tenant ID: {new_tenant_id}")

        # 2. Bu Tenant'a bağlı Admin Kullanıcısını Oluştur
        print(f"'{ADMIN_EMAIL}' adında Admin kullanıcısı oluşturuluyor...")
        existing_user = users_collection.where(filter=FieldFilter("email", "==", ADMIN_EMAIL)).limit(1).stream()
        if any(existing_user):
            print(f"Hata: '{ADMIN_EMAIL}' email adresine sahip kullanıcı zaten mevcut.")
            # Oluşturulan tenant'ı geri almak zor olacağından script'i durduruyoruz.
            # Manuel olarak silmeniz gerekebilir.
            return

        hashed_password = get_password_hash(ADMIN_PASSWORD)
        
        admin_data = {
            "email": ADMIN_EMAIL,
            "full_name": ADMIN_FULL_NAME,
            "hashed_password": hashed_password,
            "role": "Admin",
            "tenant_id": new_tenant_id  # <-- KRİTİK: Admin'i bu tenant'a bağla
        }
        
        user_ref = users_collection.document()
        user_ref.set(admin_data)
        
        print(f"Başarılı! '{ADMIN_EMAIL}' kullanıcısı '{TENANT_NAME}' (ID: {new_tenant_id}) tenant'ına Admin olarak atandı.")

    except Exception as e:
        print(f"Bir hata oluştu: {e}")

if __name__ == "__main__":
    create_admin_and_tenant()