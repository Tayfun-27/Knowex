# backend/app/services/llm_providers.py
# LLM modelleri ve wrapper'ları

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
import requests

from app.core.config import GEMINI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, OLLAMA_API_BASE_URL


class OllamaLLMWrapper(BaseChatModel):
    """Ollama API'sini LangChain uyumlu hale getiren wrapper."""
    
    base_url: str = Field(description="Ollama API base URL")
    model_name: str = Field(default="llama3", description="Ollama model adı")
    
    def __init__(self, base_url: str, model_name: str = "llama3", **kwargs):
        # URL'yi temizle
        base_url = base_url.rstrip('/')
        super().__init__(base_url=base_url, model_name=model_name, **kwargs)
        
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """LangChain'in beklediği format için messages'ı dönüştür."""
        api_url = f"{self.base_url}/api/chat"
        
        # LangChain messages formatını Ollama formatına çevir
        ollama_messages = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                ollama_messages.append({"role": "system", "content": msg.content})
            elif isinstance(msg, HumanMessage):
                ollama_messages.append({"role": "user", "content": msg.content})
            elif isinstance(msg, AIMessage):
                ollama_messages.append({"role": "assistant", "content": msg.content})
        
        payload = {
            "model": self.model_name,
            "messages": ollama_messages,
            "stream": False
        }
        
        try:
            print(f"🔷 Ollama (Llama) modeline istek gönderiliyor: {self.model_name}...")
            response = requests.post(api_url, json=payload, timeout=300)
            response.raise_for_status()
            
            data = response.json()
            content = data["message"]["content"]
            
            # Usage metadata'yı sakla (token tracking için)
            usage_info = {
                "prompt_eval_count": data.get("prompt_eval_count", 0),
                "eval_count": data.get("eval_count", 0)
            }
            
            # LangChain uyumlu AIMessage oluştur
            ai_message = AIMessage(content=content)
            
            # Usage metadata'yı response_metadata'ya ekle
            ai_message.response_metadata = {
                "usage_metadata": usage_info,
                "model": self.model_name,
                "base_url": self.base_url
            }
            
            # LangChain'in beklediği ChatResult formatını döndür
            generation = ChatGeneration(message=ai_message)
            return ChatResult(generations=[generation])
        except requests.exceptions.RequestException as e:
            print(f"❌ Ollama API hatası: {e}")
            raise Exception(f"Ollama (Llama) modeline erişilemedi: {e}") from e
    
    @property
    def _llm_type(self) -> str:
        return "ollama"


def get_llm_for_model(model_name: str) -> BaseChatModel:
    """Model adına göre uygun LLM'i döndürür."""
    print(f"🤖 Model seçiliyor: {model_name}")
    
    if model_name == "gemini":
        try:
            llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash-lite", temperature=0, google_api_key=GEMINI_API_KEY)
            print("✅ Gemini modeli yüklendi")
            return llm
        except Exception as e:
            print(f"❌ Gemini modeli yüklenemedi: {e}")
            raise Exception(f"Gemini modeli yüklenemedi: {e}") from e
    
    elif model_name == "gpt-4o":
        try:
            if not OPENAI_API_KEY:
                raise Exception("OPENAI_API_KEY yapılandırılmamış")
            llm = ChatOpenAI(model="gpt-4o", temperature=0, api_key=OPENAI_API_KEY)
            print("✅ GPT-4o modeli yüklendi")
            return llm
        except Exception as e:
            print(f"❌ GPT-4o modeli yüklenemedi: {e}")
            raise Exception(f"GPT-4o modeli yüklenemedi: {e}") from e
    
    elif model_name == "claude":
        try:
            if not ANTHROPIC_API_KEY:
                raise Exception("ANTHROPIC_API_KEY yapılandırılmamış")
            llm = ChatAnthropic(model="claude-3-haiku-20240307", temperature=0, api_key=ANTHROPIC_API_KEY)
            print("✅ Claude modeli yüklendi")
            return llm
        except Exception as e:
            print(f"❌ Claude modeli yüklenemedi: {e}")
            raise Exception(f"Claude modeli yüklenemedi: {e}") from e
    
    elif model_name == "llama":
        try:
            if not OLLAMA_API_BASE_URL:
                raise Exception(
                    "OLLAMA_API_BASE_URL yapılandırılmamış. "
                    "Lütfen environment variable'ı ayarlayın veya Ollama'nın http://localhost:11434 adresinde çalıştığından emin olun. "
                    "Ollama'yı başlatmak için terminal'de 'ollama serve' komutunu çalıştırın."
                )
            
            print(f"🔗 Ollama bağlantısı deneniyor: {OLLAMA_API_BASE_URL}")
            llm = OllamaLLMWrapper(base_url=OLLAMA_API_BASE_URL, model_name="llama3")
            print("✅ Llama (Ollama) modeli yüklendi")
            return llm
        except requests.exceptions.ConnectionError as e:
            error_msg = (
                f"Ollama sunucusuna bağlanılamadı. Lütfen Ollama'nın çalıştığından emin olun.\n"
                f"  - Ollama URL: {OLLAMA_API_BASE_URL}\n"
                f"  - Ollama'yı başlatmak için: 'ollama serve' komutunu çalıştırın\n"
                f"  - Farklı bir URL kullanmak için: OLLAMA_API_BASE_URL environment variable'ını ayarlayın"
            )
            print(f"❌ {error_msg}")
            raise Exception(error_msg) from e
        except Exception as e:
            error_msg = f"Llama modeli yüklenemedi: {e}"
            print(f"❌ {error_msg}")
            raise Exception(error_msg) from e
    
    else:
        raise Exception(f"Bilinmeyen model: {model_name}")


def get_cheap_llm() -> BaseChatModel:
    """
    Daha düşük maliyetli işlemler (Reranking, HyDE vb.) için ucuz bir model döndürür.
    Varsayılan olarak Gemini Flash kullanılır.
    """
    try:
        # Gemini Flash şu an en iyi F/P oranına sahip
        llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash-lite", temperature=0, google_api_key=GEMINI_API_KEY)
        # print("✅ Ucuz model (Gemini Flash) yüklendi")
        return llm
    except Exception as e:
        print(f"⚠️ Ucuz model yüklenemedi, fallback olarak GPT-3.5 veya mevcut diğer modeller denenebilir: {e}")
        # Fallback mekanizması eklenebilir
        raise e


