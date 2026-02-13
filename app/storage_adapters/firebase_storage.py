# backend/app/storage_adapters/firebase_storage.py
# (delete_file metodu implemente edildi)

from google.cloud import storage
from typing import IO
from app.storage_adapters.base import BaseStorageAdapter
from app.core.config import FIREBASE_STORAGE_BUCKET
import datetime

class FirebaseStorageAdapter(BaseStorageAdapter):
    """Firebase/Google Cloud Storage için depolama adaptörü."""

    def __init__(self):
        self.client = storage.Client()
        self.bucket = self.client.bucket(FIREBASE_STORAGE_BUCKET)

    def _get_blob(self, storage_path: str) -> storage.Blob:
        """Yardımcı fonksiyon: Storage path'ten blob nesnesini alır."""
        # gs://bucket-adi/tenant-id/dosya-adi formatından
        # sadece 'tenant-id/dosya-adi' kısmını al
        blob_name = storage_path.replace(f"gs://{FIREBASE_STORAGE_BUCKET}/", "")
        return self.bucket.blob(blob_name)

    def upload_file(self, file_obj: IO, tenant_id: str, file_name: str) -> str:
        """Dosyayı Firebase Storage'a yükler."""
        blob_path = f"{tenant_id}/{file_name}"
        blob = self.bucket.blob(blob_path)
        
        file_obj.seek(0)
        blob.upload_from_file(file_obj)
        
        return f"gs://{FIREBASE_STORAGE_BUCKET}/{blob_path}"

    def get_download_url(self, storage_path: str) -> str:
        """Dosya için 1 saat geçerli bir imzalı URL oluşturur."""
        blob = self._get_blob(storage_path)
        
        url = blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(hours=1),
            method="GET",
        )
        return url

    # YENİ EKLENDİ
    def download_file_content(self, storage_path: str) -> bytes:
        """Dosyanın içeriğini RAG için byte olarak indirir."""
        try:
            blob = self._get_blob(storage_path)
            content = blob.download_as_bytes()
            return content
        except Exception as e:
            print(f"Firebase Storage'dan dosya indirilirken hata: {e}")
            raise Exception("Dosya içeriği indirilemedi.")
            
    # --- YENİ EKLENDİ: Dosya Silme ---
    def delete_file(self, storage_path: str):
        """Dosyayı Firebase Storage'dan siler."""
        try:
            blob = self._get_blob(storage_path)
            blob.delete()
            print(f"Dosya başarıyla silindi: {storage_path}")
        except Exception as e:
            # Dosya zaten yoksa hata vermemesi önemli
            print(f"Firebase Storage'dan dosya silinirken hata (muhtemelen zaten yok): {e}")
    # --- BİTTİ ---