# backend/app/storage_adapters/local_storage.py
# (delete_file metodu implemente edildi)

import os
import shutil
from typing import IO
from pathlib import Path
from app.storage_adapters.base import BaseStorageAdapter
from app.core.config import LOCAL_STORAGE_PATH

class LocalStorageAdapter(BaseStorageAdapter):
    """Dosyaları sunucudaki yerel bir klasöre kaydeden adaptör."""

    def __init__(self):
        self.base_path = Path(LOCAL_STORAGE_PATH)
        os.makedirs(self.base_path, exist_ok=True)

    def upload_file(self, file_obj: IO, tenant_id: str, file_name: str) -> str:
        """Dosyayı lokal diske kaydeder."""
        tenant_path = self.base_path / tenant_id
        os.makedirs(tenant_path, exist_ok=True)
        
        file_path = tenant_path / file_name
        
        file_obj.seek(0)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file_obj, buffer)
            
        return str(file_path.resolve())

    def get_download_url(self, storage_path: str) -> str:
        """
        Lokal depolama için bu, dosyanın API üzerinden sunulacağı
        özel bir endpoint yolu olabilir. Şimdilik sadece yolu döndürelim.
        (Gelecekte /api/v1/files/download?path=... gibi bir şey gerekir)
        """
        return f"/download/{Path(storage_path).name}" # Geçici
        
    # YENİ EKLENDİ
    def download_file_content(self, storage_path: str) -> bytes:
        """Dosyanın içeriğini RAG için lokal diskten okur."""
        try:
            file_path = Path(storage_path)
            if not file_path.exists():
                raise FileNotFoundError("Dosya lokalde bulunamadı.")
            
            content = file_path.read_bytes()
            return content
        except Exception as e:
            print(f"Lokal dosyadan içerik okunurken hata: {e}")
            raise Exception("Dosya içeriği okunamadı.")
            
    # --- YENİ EKLENDİ: Dosya Silme ---
    def delete_file(self, storage_path: str):
        """Dosyayı lokal diskten siler."""
        try:
            file_path = Path(storage_path)
            if file_path.exists():
                os.remove(file_path)
                print(f"Dosya başarıyla silindi: {storage_path}")
        except Exception as e:
            print(f"Lokal dosyadan silinirken hata: {e}")
    # --- BİTTİ ---