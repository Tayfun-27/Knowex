# backend/app/services/database_query_service.py

from app.database_connectors.base import BaseDatabaseConnector
from app.services.text_to_sql_service import generate_sql_from_question
from app.services.llm_providers import get_llm_for_model
from typing import Dict, Any

def query_database(
    question: str,
    db_connector: BaseDatabaseConnector,
    model_name: str = "gemini"
) -> Dict[str, Any]:
    """Veritabanından soru-cevap yap"""
    
    try:
        # 1. Text-to-SQL: Soruyu SQL'e çevir
        sql_query = generate_sql_from_question(question, db_connector, model_name)
        print(f"🔍 Oluşturulan SQL sorgusu: {sql_query}")
        
        # 2. SQL'i çalıştır
        results = db_connector.execute_query(sql_query)
        print(f"📊 Sorgu sonucu: {len(results)} satır döndü")
        
        # 3. Sonuçları LLM ile doğal dile çevir
        llm = get_llm_for_model(model_name)
        
        # Sonuçları formatla (çok fazla satır varsa sınırla)
        results_preview = results[:50]  # İlk 50 satırı göster
        results_text = format_results_for_prompt(results_preview)
        
        if len(results) > 50:
            results_text += f"\n\n(Not: Toplam {len(results)} satır var, sadece ilk 50 satır gösteriliyor)"
        
        prompt = f"""Aşağıdaki veritabanı sorgu sonuçlarına göre kullanıcının sorusunu cevapla.

KULLANICI SORUSU: {question}

SQL SORGUSU: {sql_query}

SORGU SONUÇLARI:
{results_text}

GÖREV:
1. Sorgu sonuçlarını analiz et
2. Kullanıcının sorusunu cevapla
3. Sonuçları anlaşılır bir şekilde sun
4. Eğer sonuç yoksa, bunu belirt
5. Sayısal sonuçlar varsa, bunları vurgula
6. Cevaplarınızı mümkün olduğunca kısa, öz ve net tutun. Gereksiz açıklamalardan kaçının.

CEVAP FORMATI KURALLARI (ÇOK ÖNEMLİ):
- ASLA tek kelimelik cevap verme (örn: "carlas" YANLIŞ!)
- MUTLAKA tam cümle kur (örn: "carlas firmasından alınmış" DOĞRU!)
- "nereden", "kimden", "hangi firmadan" gibi sorular için: "X firmasından alınmış", "X'ten satın alınmış", "X firmasından temin edilmiş" gibi doğal cümleler kullan
- Firma/şirket isimleri için: "X firması", "X şirketi", "X A.Ş." gibi tam ifadeler kullan
- Ürün isimleri için: "X ürünü", "X malzemesi" gibi tam ifadeler kullan
- Tarih bilgileri için: "X tarihinde", "X'te" gibi bağlamlı ifadeler kullan
- Sayısal değerler için: "X adet", "X birim", "toplam X" gibi açıklayıcı ifadeler kullan
- Örnek DOĞRU cevaplar:
  * "carlas firmasından alınmış"
  * "SBR malzemesi carlas firmasından temin edilmiştir"
  * "Toplam 5 farklı tedarikçiden alım yapılmış"
  * "15.03.2025 tarihinde carlas firmasından SBR ürünü alınmış"
- Örnek YANLIŞ cevaplar:
  * "carlas" (tek kelime - YANLIŞ!)
  * "5" (sadece sayı - YANLIŞ!)
  * "SBR" (sadece ürün adı - YANLIŞ!)

CEVAP:"""

        response = llm.invoke(prompt)
        answer = response.content.strip()
        
        return {
            "answer": answer,
            "sql_query": sql_query,
            "raw_results": results,
            "row_count": len(results)
        }
    except Exception as e:
        error_message = f"Veritabanı sorgusu sırasında hata oluştu: {str(e)}"
        print(f"❌ {error_message}")
        return {
            "answer": error_message,
            "sql_query": None,
            "raw_results": [],
            "row_count": 0,
            "error": str(e)
        }

def format_results_for_prompt(results: list) -> str:
    """Sorgu sonuçlarını prompt için formatla"""
    if not results:
        return "Sonuç bulunamadı."
    
    if len(results) == 0:
        return "Sorgu sonucu boş."
    
    # İlk sonucu örnek olarak göster
    lines = []
    lines.append(f"Toplam {len(results)} satır:")
    lines.append("")
    
    # İlk birkaç satırı göster
    for i, row in enumerate(results[:10], 1):
        lines.append(f"Satır {i}:")
        for key, value in row.items():
            lines.append(f"  {key}: {value}")
        lines.append("")
    
    if len(results) > 10:
        lines.append(f"... ve {len(results) - 10} satır daha")
    
    return "\n".join(lines)

