# backend/app/services/text_to_sql_service.py

from app.database_connectors.base import BaseDatabaseConnector
from app.services.llm_providers import get_llm_for_model
import json

def generate_sql_from_question(
    question: str,
    db_connector: BaseDatabaseConnector,
    model_name: str = "gemini"
) -> str:
    """Kullanıcı sorusunu SQL sorgusuna çevir"""
    
    # Veritabanı şemasını al (bağlantı yapıldığında şema zaten alınmış olmalı)
    print("📊 Veritabanı şeması alınıyor...")
    schema = db_connector.get_schema()
    
    if not schema or (not schema.get('tables') and not schema.get('collections')):
        raise ValueError("Veritabanı şeması alınamadı veya tablo bulunamadı. Lütfen bağlantıyı kontrol edin.")
    
    print(f"✅ Şema alındı: {len(schema.get('tables', []))} tablo bulundu")
    
    llm = get_llm_for_model(model_name)
    
    # Şemayı okunabilir formata çevir
    schema_text = format_schema_for_prompt(schema)
    
    # Debug: Şema önizlemesi
    print(f"📋 Şema önizlemesi (ilk 500 karakter):\n{schema_text[:500]}...")
    
    prompt = f"""Sen bir SQL sorgu uzmanısın. Kullanıcının sorusunu veritabanı şemasına göre SQL sorgusuna çevir.

VERİTABANI ŞEMASI:
{schema_text}

KULLANICI SORUSU: {question}

GÖREV:
1. Kullanıcının sorusunu analiz et
2. Hangi tabloları ve kolonları kullanman gerektiğini belirle
3. Uygun SQL SELECT sorgusunu oluştur
4. SADECE SQL sorgusunu döndür, açıklama yapma
5. Sadece SELECT sorguları kullan (INSERT, UPDATE, DELETE, DROP vb. YASAK)

KRİTİK KURALLAR - MUTLAKA UY:
1. Sadece SELECT sorguları kullan (INSERT, UPDATE, DELETE, DROP vb. YASAK)

2. TABLO İSİMLERİNİ KULLANIRKEN:
   - Şemada "SQL'de kullan: FROM users" yazıyorsa → FROM users kullan
   - Şemada "SQL'de kullan: FROM schema.tablo" yazıyorsa → FROM schema.tablo kullan
   - ASLA "tablo.public" formatı kullanma (ÖRNEK: users.public YANLIŞ!)
   - ASLA "public.tablo" formatı kullanma (eğer şemada sadece "tablo" yazıyorsa)
   - Şemadaki "SQL'de kullan" satırındaki formatı TAM OLARAK kopyala

3. KOLON İSİMLERİ:
   - Şemadaki kolon isimlerini TAM OLARAK kullan
   - Büyük/küçük harf duyarlılığına dikkat et

4. TARİH SORGULARI:
   - PostgreSQL fonksiyonları kullan:
     * CURRENT_DATE - INTERVAL '2 day' (2 gün önce)
     * CURRENT_DATE - INTERVAL '1 week' (1 hafta önce)
     * DATE(created_at) veya created_at::date
   - Tarih kolonlarını şemadan kontrol et

5. SADECE SQL SORGUSUNU DÖNDÜR:
   - Açıklama yazma
   - Markdown kullanma
   - Sadece SQL sorgusu

SQL SORGUSU:"""

    try:
        response = llm.invoke(prompt)
        raw_sql = response.content.strip()
        print(f"🔍 LLM'den gelen ham SQL: {raw_sql[:200]}")
        
        # SQL sorgusunu temizle (açıklamaları, markdown kod bloklarını kaldır)
        sql_query = clean_sql_query(raw_sql)
        print(f"🔍 Temizlenmiş SQL: {sql_query[:200]}")
        
        # SQL injection koruması için akıllı validasyon
        import re
        
        # Çok satırlı sorgular için normalize et (boşlukları tek boşluğa çevir ama yapıyı koru)
        sql_normalized = re.sub(r'\s+', ' ', sql_query).strip()
        sql_upper = sql_normalized.upper()
        print(f"🔍 Normalize edilmiş SQL: {sql_normalized[:200]}")
        
        # SELECT kelimesini bul (başta olmasa bile)
        select_match = re.search(r'\bSELECT\b', sql_upper, re.IGNORECASE)
        if not select_match:
            # Debug: SQL sorgusunu yazdır
            print(f"⚠️ SELECT bulunamadı. SQL sorgusu: {sql_query[:200]}")
            print(f"⚠️ Normalize edilmiş: {sql_normalized[:200]}")
            raise ValueError("Güvenlik: Sadece SELECT sorgularına izin verilir")
        
        print(f"✅ SELECT bulundu, pozisyon: {select_match.start()}-{select_match.end()}")
        
        # SELECT'ten önce başka SQL komutları var mı kontrol et
        before_select = sql_upper[:select_match.start()].strip()
        print(f"🔍 SELECT'ten önce: '{before_select}'")
        if before_select:
            # SELECT'ten önce sadece boşluk olmalı
            dangerous_before = ['DROP', 'DELETE', 'TRUNCATE', 'ALTER', 'CREATE', 'INSERT', 'UPDATE', 'GRANT', 'REVOKE', 'EXEC', 'EXECUTE', 'MERGE', 'CALL']
            for cmd in dangerous_before:
                if re.search(r'\b' + cmd + r'\b', before_select):
                    raise ValueError(f"Güvenlik: SELECT'ten önce tehlikeli komut tespit edildi: {cmd}")
        
        # SELECT'ten sonraki kısmı al (SELECT kelimesinin sonundan itibaren)
        sql_after_select = sql_normalized[select_match.end():].strip()
        sql_upper_after = sql_after_select.upper()
        print(f"🔍 SELECT'ten sonra: '{sql_after_select[:100]}'")
        
        # SELECT'ten sonra tehlikeli komutlar var mı kontrol et
        # Ancak SELECT içindeki alt sorguları (subqueries) hariç tut
        dangerous_patterns = [
            r'\bDROP\b',
            r'\bDELETE\b',
            r'\bTRUNCATE\b',
            r'\bALTER\b',
            r'\bCREATE\b',
            r'\bINSERT\b',
            r'\bUPDATE\b',
            r'\bGRANT\b',
            r'\bREVOKE\b',
            r'\bEXEC\b',
            r'\bEXECUTE\b',
            r'\bMERGE\b',
            r'\bCALL\b'
        ]
        
        for pattern in dangerous_patterns:
            match = re.search(pattern, sql_upper_after)
            if match:
                print(f"⚠️ Tehlikeli komut bulundu: {pattern} (pozisyon: {match.start()})")
                raise ValueError(f"Güvenlik: Tehlikeli SQL komutu tespit edildi: {pattern}")
        
        print(f"✅ Güvenlik kontrolü başarılı, SQL sorgusu onaylandı")
        # Temizlenmiş sorguyu döndür (orijinal formatı koru)
        return sql_query
    except Exception as e:
        print(f"SQL oluşturma hatası: {e}")
        raise

def clean_sql_query(sql_query: str) -> str:
    """SQL sorgusunu temizle - markdown kod blokları, açıklamalar vb. kaldır"""
    import re
    
    # Markdown kod bloklarını kaldır (```sql ... ```)
    sql_query = re.sub(r'```sql\s*', '', sql_query, flags=re.IGNORECASE)
    sql_query = re.sub(r'```\s*', '', sql_query)
    
    # SQL yorumlarını kaldır (-- ve /* */)
    sql_query = re.sub(r'--.*?$', '', sql_query, flags=re.MULTILINE)
    sql_query = re.sub(r'/\*.*?\*/', '', sql_query, flags=re.DOTALL)
    
    # Başta ve sonda boşlukları temizle
    sql_query = sql_query.strip()
    
    # SELECT kelimesini bul (başta olmasa bile, ama ilk SQL komutu olmalı)
    select_match = re.search(r'\bSELECT\b', sql_query, re.IGNORECASE)
    if select_match:
        # SELECT'ten önce tehlikeli komutlar var mı kontrol et
        before_select = sql_query[:select_match.start()].strip()
        if before_select:
            before_upper = before_select.upper()
            # SELECT'ten önce sadece boşluk/yeni satır olmalı, başka komut olmamalı
            dangerous_before = ['DROP', 'DELETE', 'TRUNCATE', 'ALTER', 'CREATE', 'INSERT', 'UPDATE', 'GRANT', 'REVOKE', 'EXEC', 'EXECUTE', 'MERGE', 'CALL']
            for cmd in dangerous_before:
                if re.search(r'\b' + cmd + r'\b', before_upper):
                    # SELECT'ten önce tehlikeli komut var, onu kaldır
                    sql_query = sql_query[select_match.start():]
                    break
    
    return sql_query.strip()

def format_schema_for_prompt(schema: dict) -> str:
    """Veritabanı şemasını prompt için okunabilir formata çevir"""
    if not schema:
        return "Şema bilgisi bulunamadı."
    
    lines = []
    
    if 'database_name' in schema:
        lines.append(f"Veritabanı: {schema['database_name']}")
        lines.append("")
    
    if 'tables' in schema:
        lines.append("=" * 60)
        lines.append("TABLOLAR VE KOLONLAR:")
        lines.append("=" * 60)
        lines.append("")
        lines.append("ÖNEMLİ: Tablo isimlerini TAM OLARAK aşağıdaki gibi kullan!")
        lines.append("")
        
        for table in schema['tables']:
            table_name = table['name']
            table_schema = table.get('schema', 'public')
            
            # Tablo adını net bir şekilde göster
            # Eğer table_name zaten schema.tablo formatındaysa, onu kullan
            # Değilse, schema'ya göre formatla
            if '.' in table_name:
                # Zaten schema.tablo formatında
                actual_table_name = table_name.split('.')[-1]  # Son kısım tablo adı
                actual_schema = table_name.split('.')[0]  # İlk kısım schema
                
                if actual_schema == 'public':
                    # public.tablo formatından sadece tablo adını al
                    sql_table_name = actual_table_name
                    lines.append(f"📋 TABLO: {actual_table_name} (Schema: public)")
                    lines.append(f"   SQL'de kullan: FROM {sql_table_name}")
                    lines.append(f"   ❌ YANLIŞ: FROM {sql_table_name}.public")
                    lines.append(f"   ❌ YANLIŞ: FROM public.{sql_table_name}")
                else:
                    # Farklı schema
                    sql_table_name = table_name
                    lines.append(f"📋 TABLO: {sql_table_name} (Schema: {actual_schema})")
                    lines.append(f"   SQL'de kullan: FROM {sql_table_name}")
            elif table_schema == 'public':
                # Public schema için sadece tablo adı
                lines.append(f"📋 TABLO: {table_name} (Schema: public)")
                lines.append(f"   SQL'de kullan: FROM {table_name}")
                lines.append(f"   ❌ YANLIŞ: FROM {table_name}.public")
                lines.append(f"   ❌ YANLIŞ: FROM public.{table_name}")
            else:
                # Diğer schema'lar için schema.tablo formatı
                sql_table_name = f"{table_schema}.{table_name}"
                lines.append(f"📋 TABLO: {sql_table_name} (Schema: {table_schema})")
                lines.append(f"   SQL'de kullan: FROM {sql_table_name}")
            
            lines.append("")
            lines.append("   KOLONLAR:")
            if 'columns' in table and table['columns']:
                for col in table['columns']:
                    nullable = "NULL" if col.get('nullable', True) else "NOT NULL"
                    col_type = col.get('type', 'unknown')
                    lines.append(f"     • {col['name']} ({col_type}) {nullable}")
            else:
                lines.append("     (Kolon bilgisi yok)")
            lines.append("")
            lines.append("-" * 60)
            lines.append("")
    elif 'collections' in schema:
        lines.append("KOLEKSİYONLAR:")
        for collection in schema['collections']:
            lines.append(f"  Koleksiyon: {collection['name']}")
            if 'sample_fields' in collection:
                lines.append(f"    Alanlar: {', '.join(collection['sample_fields'])}")
            lines.append("")
    
    return "\n".join(lines)

