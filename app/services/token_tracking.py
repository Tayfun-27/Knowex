# backend/app/services/token_tracking.py
# Token kullanım takip sistemi

from typing import List, Dict, Any, Tuple


class TokenTracker:
    """Her LLM çağrısının token kullanımını izler ve toplar."""
    def __init__(self):
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_tokens = 0
        self.call_details: List[Dict[str, Any]] = []
        
    def add_usage(self, input_tokens: int, output_tokens: int, step_name: str, 
                  estimated: bool = False, raw_metadata: Dict[str, Any] = None):
        """Token kullanımını ekle."""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_tokens = self.total_input_tokens + self.total_output_tokens
        
        detail = {
            "step": step_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total": input_tokens + output_tokens,
            "estimated": estimated,
            "metadata": raw_metadata
        }
        self.call_details.append(detail)
        
        print(f"\n📊 TOKEN KULLANIMI ({step_name}):")
        print(f"   Giriş: {input_tokens:,} | Çıkış: {output_tokens:,} | Toplam: {input_tokens + output_tokens:,}")
        if estimated:
            print(f"   ⚠️ Bu değerler tahminidir (metadata'dan alınamadı)")
        print(f"   📈 TOPLAM: Giriş={self.total_input_tokens:,} | Çıkış={self.total_output_tokens:,} | Toplam={self.total_tokens:,}\n")
    
    def get_summary(self) -> Dict[str, Any]:
        """Token kullanım özetini döndür."""
        # Gemini 1.5 Flash / 2.0 Flash Fiyatlandırması (Yaklaşık):
        # Girdi (input): $0.10 per 1M tokens
        # Çıktı (output): $0.40 per 1M tokens
        # NOT: Fiyatlar değişebilir, Google Cloud Pricing sayfasını kontrol edin.
        input_cost_per_million = 0.10
        output_cost_per_million = 0.40
        
        input_cost = (self.total_input_tokens / 1_000_000) * input_cost_per_million
        output_cost = (self.total_output_tokens / 1_000_000) * output_cost_per_million
        total_cost_usd = input_cost + output_cost
        
        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "call_count": len(self.call_details),
            "breakdown": self.call_details,
            "estimated_cost_usd": total_cost_usd,
            "estimated_cost_tl": total_cost_usd * 35  # ~35 TL/USD
        }


def estimate_tokens_from_text(text: str) -> int:
    """Metinden token sayısını tahmin et (Google Gemini için ~4 karakter = 1 token)."""
    if not text:
        return 0
    # Türkçe karakterler daha az token kullanır, İngilizce daha fazla
    # Ortalama olarak ~3-4 karakter = 1 token alıyoruz
    return max(1, len(text) // 3)


def extract_token_usage_from_response(response, step_name: str, prompt_text: str = None) -> Tuple[int, int]:
    """LangChain response'dan token bilgisini çıkar. Başarısız olursa tahmin yap."""
    input_tokens, output_tokens = 0, 0
    
    # Debug için raw metadata'yı yazdır
    if hasattr(response, 'response_metadata'):
        print(f"🔍 DEBUG ({step_name}) Raw Metadata: {response.response_metadata}")
    elif hasattr(response, 'usage_metadata'):
        print(f"🔍 DEBUG ({step_name}) Usage Metadata: {response.usage_metadata}")
    
    # 1. Önce AIMessage'ın direkt usage_metadata özelliğini kontrol et
    if hasattr(response, 'usage_metadata') and response.usage_metadata:
        usage = response.usage_metadata
        if isinstance(usage, dict):
            input_tokens = usage.get('prompt_token_count', usage.get('input_tokens', 0))
            output_tokens = usage.get('candidates_token_count', usage.get('output_tokens', 0))
            # OpenAI formatı
            if input_tokens == 0 and 'input_tokens' in usage:
                input_tokens = usage['input_tokens']
            if output_tokens == 0 and 'output_tokens' in usage:
                output_tokens = usage['output_tokens']
                
            if input_tokens > 0 or output_tokens > 0:
                print(f"✅ ({step_name}): Token bilgisi 'usage_metadata' özelliğinden alındı.")
                return input_tokens, output_tokens
        elif hasattr(usage, 'prompt_token_count'):
            input_tokens = usage.prompt_token_count
            output_tokens = getattr(usage, 'candidates_token_count', getattr(usage, 'completion_token_count', 0))
            if input_tokens > 0 or output_tokens > 0:
                print(f"✅ ({step_name}): Token bilgisi 'usage_metadata' nesnesinden alındı.")
                return input_tokens, output_tokens
    
    # 2. response_metadata içinde usage_metadata kontrolü
    if hasattr(response, 'response_metadata') and response.response_metadata:
        metadata = response.response_metadata
        
        # usage_metadata kontrolü (Google Gemini formatı)
        if 'usage_metadata' in metadata:
            gemini_usage = metadata.get('usage_metadata', {})
            # Ollama formatı için prompt_eval_count ve eval_count kontrolü
            if 'prompt_eval_count' in gemini_usage or 'eval_count' in gemini_usage:
                input_tokens = gemini_usage.get('prompt_eval_count', 0)
                output_tokens = gemini_usage.get('eval_count', 0)
                if input_tokens > 0 or output_tokens > 0:
                    print(f"✅ ({step_name}): Token bilgisi 'response_metadata.usage_metadata' içinden alındı (Ollama formatı).")
                    return input_tokens, output_tokens
            
            # Gemini formatı
            input_tokens = gemini_usage.get('prompt_token_count', 0)
            output_tokens = gemini_usage.get('candidates_token_count', 0)
            
            # Eğer hala 0 ise, total_token_count'a bak (bazı versiyonlarda sadece bu olabilir)
            if input_tokens == 0 and output_tokens == 0 and 'total_token_count' in gemini_usage:
                total = gemini_usage.get('total_token_count', 0)
                # Tahmini dağılım yap (input genellikle daha çoktur RAG'de)
                if total > 0:
                    print(f"⚠️ ({step_name}): Sadece toplam token var, tahmini dağıtılıyor.")
                    input_tokens = int(total * 0.8)
                    output_tokens = total - input_tokens
            
            if input_tokens > 0 or output_tokens > 0:
                print(f"✅ ({step_name}): Token bilgisi 'response_metadata.usage_metadata' içinden alındı.")
                return input_tokens, output_tokens
        
        # token_usage kontrolü (genel format / OpenAI)
        if 'token_usage' in metadata:
            usage = metadata.get('token_usage', {})
            input_tokens = usage.get('prompt_tokens', usage.get('input_tokens', 0))
            output_tokens = usage.get('completion_tokens', usage.get('output_tokens', 0))
            if input_tokens > 0 or output_tokens > 0:
                print(f"✅ ({step_name}): Token bilgisi 'response_metadata.token_usage' içinden alındı.")
                return input_tokens, output_tokens
                
        # Anthropic formatı (usage)
        if 'usage' in metadata:
            usage = metadata.get('usage', {})
            input_tokens = usage.get('input_tokens', 0)
            output_tokens = usage.get('output_tokens', 0)
            if input_tokens > 0 or output_tokens > 0:
                print(f"✅ ({step_name}): Token bilgisi 'response_metadata.usage' içinden alındı (Anthropic).")
                return input_tokens, output_tokens
    
    # 3. Eğer metadata'dan alınamadıysa, metin uzunluğuna göre tahmin yap
    # Output token tahmini (response content'ten)
    if hasattr(response, 'content') and response.content:
        estimated_output = estimate_tokens_from_text(response.content)
    else:
        estimated_output = 0
    
    # Input token tahmini (prompt'tan)
    if prompt_text:
        estimated_input = estimate_tokens_from_text(prompt_text)
    else:
        # Prompt bilgisi yoksa, response'dan geriye doğru tahmin yap
        # Genellikle input, output'un 5-10 katı olabilir (uzun promptlar için)
        estimated_input = estimated_output * 5 if estimated_output > 0 else 0
    
    # Eğer hiç token yoksa, en azından küçük bir değer ver
    if estimated_input == 0 and estimated_output == 0:
        print(f"⚠️ UYARI ({step_name}): Token bilgisi hiçbir şekilde alınamadı!")
        return 0, 0
    
    print(f"⚠️ UYARI ({step_name}): Metadata'dan token bilgisi alınamadı, tahmin kullanılıyor.")
    print(f"   Tahmin: Giriş={estimated_input}, Çıkış={estimated_output}")
    return estimated_input, estimated_output

