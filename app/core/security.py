# backend/app/core/security.py
# (Toplam ~50 satır)

from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from app.core.config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES
from fastapi import HTTPException, status
from app.schemas.user import TokenData

# Şifreleme bağlamı (bcrypt algoritmasını kullan)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Kullanıcının girdiği düz metin şifreyi, veritabanındaki
    hash'lenmiş şifre ile karşılaştırır.
    """
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """
    Kullanıcının girdiği düz metin şifreyi hash'ler.
    Veritabanına bu değeri kaydedeceğiz.
    """
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """
    Verilen 'data' (örn: {"sub": "user@example.com"}) için
    bir JWT Erişim Token'ı oluşturur.
    """
    to_encode = data.copy()
    
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        # Yapılandırmadan varsayılan süre (örn: 1 gün)
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=ACCESS_TOKEN_EXPIRE_MINUTES
        )
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt
def decode_access_token(token: str) -> TokenData:
    """
    Gönderilen JWT token'ını çözer ve içindeki veriyi (TokenData) döndürür.
    Eğer token geçersiz veya süresi dolmuşsa hata fırlatır.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Kimlik bilgileri doğrulanamadı",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # Token'ı, gizli anahtarımız (SECRET_KEY) ve algoritmamız (ALGORITHM)
        # ile çözmeyi dene
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        
        # Token'ın 'sub' (subject) alanını al (biz buraya email'i koymuştuk)
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
        
        # Token verisini Pydantic modeli olarak döndür
        return TokenData(email=email)
    except JWTError:
        # Token geçersizse (süre dolmuş, imza yanlış vb.)
        raise credentials_exception