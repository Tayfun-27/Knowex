# backend/app/core/chunker.py

from typing import List

# Basit bir metin bölücü (chunker)
# Daha gelişmiş kütüphaneler (örn: LangChain) de kullanılabilir
# ama temel ihtiyaç için bu yeterlidir.

def get_text_chunks(text: str, chunk_size: int = 1000, overlap: int = 150) -> List[str]:
    """
    Verilen bir metni, belirlenen boyut ve örtüşme (overlap)
    oranına göre parçalara (chunk) ayırır.
    
    Overlap, anlamsal bütünlüğün kaybolmaması için önemlidir.
    """
    if not text:
        return []
        
    chunks = []
    start = 0
    
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        
        # Eğer son parçaya geldiysek dur
        if end >= len(text):
            break
            
        # Overlap'i dikkate alarak bir sonraki başlangıç noktasını belirle
        start += (chunk_size - overlap)
        
    return chunks