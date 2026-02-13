# backend/app/storage_adapters/base.py
# (delete_file metodu eklendi)

from abc import ABC, abstractmethod
from typing import IO

class BaseStorageAdapter(ABC):
    """
    Tüm depolama yöntemleri (Lokal, Firebase, S3 vb.)
    için soyut temel sınıf (arayüz).
    """

    @abstractmethod
    def upload_file(
        self,
        file_obj: IO,           # Yüklenecek dosya nesnesi
        tenant_id: str,       # Hangi firmaya ait olduğu (silo için)
        file_name: str        # Kaydedilecek dosya adı
    ) -> str:
        """
        Dosyayı depolama alanına yükler ve
        depolandığı yolu (storage_path) string olarak döndürür.
        """
        pass

    @abstractmethod
    def get_download_url(self, storage_path: str) -> str:
        """
        Depolama yolunu, indirilebilir bir URL'e çevirir.
        (Lokal için dosya yolu, bulut için imzalı URL olabilir)
        """
        pass

    # YENİ EKLENDİ
    @abstractmethod
    def download_file_content(self, storage_path: str) -> bytes:
        """
        Depolama yolundaki dosyanın ham içeriğini 'bytes' olarak döndürür.
        RAG (Yapay Zeka) için kullanılacak.
        """
        pass
        
    # --- YENİ EKLENDİ: Dosya Silme ---
    @abstractmethod
    def delete_file(self, storage_path: str):
        """
        Verilen yoldaki dosyayı depolama alanından (storage) siler.
        """
        pass
    # --- BİTTİ ---