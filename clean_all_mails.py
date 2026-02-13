# backend/clean_all_mails.py
# TÜM MAİLLERİ TEMİZLEME SCRIPTİ - TEK SEFERLİK KULLANIM
# DİKKAT: Bu script tüm mailleri, attachment'ları, vektör verilerini ve mail conversations'ları siler!

import sys
import os

# Proje root'unu path'e ekle
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from google.cloud import firestore
from google.cloud import storage
from app.core.config import FIREBASE_STORAGE_BUCKET
from app.storage_adapters.firebase_storage import FirebaseStorageAdapter

def clean_all_mails():
    """Tüm mailleri, attachment'ları, vektör verilerini ve conversations'ları temizler."""
    
    print("=" * 60)
    print("🚨 TÜM MAİLLERİ TEMİZLEME İŞLEMİ BAŞLATILIYOR 🚨")
    print("=" * 60)
    
    # Onay iste
    confirmation = input("\n⚠️  DİKKAT: Bu işlem GERİ ALINAMAZ!\n"
                        "Tüm mailler, attachment'lar, vektör verileri ve mail conversations silinecek.\n"
                        "Devam etmek istediğinizden emin misiniz? (EVET yazın): ")
    
    if confirmation != "EVET":
        print("❌ İşlem iptal edildi.")
        return
    
    firestore_db = firestore.Client()
    storage_client = storage.Client()
    bucket = storage_client.bucket(FIREBASE_STORAGE_BUCKET)
    storage_adapter = FirebaseStorageAdapter()
    
    # 1. Mailleri al ve attachment'ları topla
    mail_col = firestore_db.collection("mails")
    all_mails = list(mail_col.stream())
    
    print(f"\n📧 Toplam {len(all_mails)} mail bulundu.")
    
    if len(all_mails) == 0:
        print("✅ Silinecek mail yok.")
        return
    
    # 2. Attachment'ları ve mail ID'lerini topla
    mail_ids = []
    attachment_paths = []
    deleted_attachments = 0
    failed_attachments = 0
    
    for mail_doc in all_mails:
        mail_data = mail_doc.to_dict()
        mail_id = mail_doc.id
        mail_ids.append(mail_id)
        
        # Attachment path'lerini topla
        attachments = mail_data.get("attachments", [])
        for att_path in attachments:
            if att_path:
                attachment_paths.append(att_path)
    
    print(f"📎 Toplam {len(attachment_paths)} attachment bulundu.")
    
    # 3. Attachment'ları sil
    print("\n🗑️  Attachment'lar siliniyor...")
    for att_path in attachment_paths:
        try:
            storage_adapter.delete_file(att_path)
            deleted_attachments += 1
            if deleted_attachments % 10 == 0:
                print(f"   ✅ {deleted_attachments}/{len(attachment_paths)} attachment silindi...")
        except Exception as e:
            failed_attachments += 1
            print(f"   ⚠️  Attachment silinemedi ({att_path}): {e}")
    
    print(f"✅ {deleted_attachments} attachment başarıyla silindi.")
    if failed_attachments > 0:
        print(f"⚠️  {failed_attachments} attachment silinemedi (muhtemelen zaten yok).")
    
    # 4. Storage'dan mail_attachments klasörlerini de temizle (ekstra güvenlik)
    print("\n🗑️  Storage'daki mail_attachments klasörleri temizleniyor...")
    try:
        blobs = bucket.list_blobs(prefix="")
        mail_attachment_blobs = [blob for blob in blobs if "mail_attachments" in blob.name]
        
        deleted_folders = 0
        for blob in mail_attachment_blobs:
            try:
                blob.delete()
                deleted_folders += 1
            except Exception as e:
                print(f"   ⚠️  Blob silinemedi ({blob.name}): {e}")
        
        print(f"✅ {deleted_folders} mail attachment blob'u silindi.")
    except Exception as e:
        print(f"⚠️  Storage klasör temizleme hatası: {e}")
    
    # 5. Vektör veritabanındaki mail chunk'larını sil
    print("\n🗑️  Vektör veritabanındaki mail chunk'ları siliniyor...")
    chunks_col = firestore_db.collection("text_chunks")
    deleted_chunks = 0
    
    for mail_id in mail_ids:
        # file_id = "mail_{mail_id}" formatındaki chunk'ları bul
        query = chunks_col.where(filter=firestore.FieldFilter("file_id", "==", f"mail_{mail_id}"))
        chunks = list(query.stream())
        
        for chunk_doc in chunks:
            try:
                chunk_doc.reference.delete()
                deleted_chunks += 1
            except Exception as e:
                print(f"   ⚠️  Chunk silinemedi ({chunk_doc.id}): {e}")
    
    print(f"✅ {deleted_chunks} chunk başarıyla silindi.")
    
    # 6. Mail conversations'ları sil
    print("\n🗑️  Mail conversations siliniyor...")
    conv_col = firestore_db.collection("mail_conversations")
    all_conversations = list(conv_col.stream())
    deleted_conversations = 0
    
    for conv_doc in all_conversations:
        try:
            conv_doc.reference.delete()
            deleted_conversations += 1
        except Exception as e:
            print(f"   ⚠️  Conversation silinemedi ({conv_doc.id}): {e}")
    
    print(f"✅ {deleted_conversations} conversation başarıyla silindi.")
    
    # 7. Mailleri sil
    print("\n🗑️  Mailler Firestore'dan siliniyor...")
    deleted_mails = 0
    
    for mail_doc in all_mails:
        try:
            mail_doc.reference.delete()
            deleted_mails += 1
            if deleted_mails % 10 == 0:
                print(f"   ✅ {deleted_mails}/{len(all_mails)} mail silindi...")
        except Exception as e:
            print(f"   ⚠️  Mail silinemedi ({mail_doc.id}): {e}")
    
    print(f"✅ {deleted_mails} mail başarıyla silindi.")
    
    # Özet
    print("\n" + "=" * 60)
    print("📊 TEMİZLEME ÖZETİ")
    print("=" * 60)
    print(f"✅ Silinen mailler: {deleted_mails}")
    print(f"✅ Silinen attachment'lar: {deleted_attachments}")
    print(f"✅ Silinen chunk'lar: {deleted_chunks}")
    print(f"✅ Silinen conversations: {deleted_conversations}")
    print("=" * 60)
    print("🎉 Temizleme işlemi tamamlandı!")

if __name__ == "__main__":
    try:
        clean_all_mails()
    except KeyboardInterrupt:
        print("\n\n❌ İşlem kullanıcı tarafından iptal edildi.")
    except Exception as e:
        print(f"\n\n❌ Hata oluştu: {e}")
        import traceback
        traceback.print_exc()