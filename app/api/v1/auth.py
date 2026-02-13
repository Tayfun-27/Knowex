# backend/app/api/v1/auth.py
# (PRINT ifadeleri daha da belirginleştirildi)

print("\n>>> app/api/v1/auth.py dosyası YÜKLENDİ (import edildi). <<<")

from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm
from app.services import auth_service
from app.schemas.user import UserCreate, UserOut, Token, UserInDB, SetPasswordRequest,ChangePasswordRequest
from app.dependencies import get_db_repository,get_current_user
from app.repositories.base import BaseRepository
from app.core.security import create_access_token
from slowapi import Limiter
from slowapi.util import get_remote_address
from app.core.config import ENVIRONMENT, DEBUG

def safe_error_message(e: Exception, default_message: str) -> str:
    """Production'da hassas bilgi sızıntısını önlemek için güvenli hata mesajı döndürür."""
    if ENVIRONMENT == "production" and not DEBUG:
        return default_message
    else:
        return f"{default_message}: {str(e)}"

limiter = Limiter(key_func=get_remote_address)

router = APIRouter()
print(">>> app/api/v1/auth.py: Auth Router oluşturuldu. <<<")

# ... (register ve login fonksiyonları aynı kalabilir) ...
@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("3/hour")  # Rate limiting: 3 kayıt/saat
def register_new_user(
    request: Request,
    user_create: UserCreate,
    db: BaseRepository = Depends(get_db_repository)
):
    try:
        new_user = auth_service.register_user(user_create, db)
        return UserOut.model_validate(new_user)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail=safe_error_message(e, "Kayıt sırasında bir hata oluştu")
        )

@router.post("/login", response_model=Token)
@limiter.limit("5/minute")  # Rate limiting: 5 istek/dakika
def login_for_access_token(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: BaseRepository = Depends(get_db_repository)
):
    try:
        user = auth_service.authenticate_user(
            email=form_data.username,
            password=form_data.password,
            db=db
        )
        access_token = create_access_token(
            data={"sub": user.email, "role": user.role}
        )
        return {"access_token": access_token, "token_type": "bearer"}
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=safe_error_message(e, "Giriş sırasında bir hata oluştu")
        )

@router.post("/set-password", response_model=UserOut)
def set_new_password(
    request: SetPasswordRequest,
    db: BaseRepository = Depends(get_db_repository)
):
    """
    Geçerli bir token ile kullanıcının şifresini (ilk kez) belirler
    veya sıfırlar.
    """
    
    # --- DETAYLI LOGLAMA EKLENDİ ---
    print("\n" + "="*50)
    print(">>> ADIM 1: /set-password ENDPOINT'İ ÇAĞRILDI (auth.py) <<<")
    print(f"Gelen Token (kısmi): {request.token[:10]}...")
    print(">>> auth_service.reset_password_with_token çağrılıyor...")
    # --- LOGLAMA BİTTİ ---
    
    try:
        updated_user = auth_service.reset_password_with_token(
            token=request.token,
            new_password=request.new_password,
            db=db
        )
        # --- DETAYLI LOGLAMA EKLENDİ ---
        print(">>> ADIM 1 BAŞARILI: Auth servisi yanıt döndü, cevap gönderiliyor. (auth.py) <<<")
        print("="*50 + "\n")
        # --- LOGLAMA BİTTİ ---
        return UserOut.model_validate(updated_user)
    
    except HTTPException as e:
        # --- DETAYLI LOGLAMA EKLENDİ ---
        print(f"!!! ADIM 1 HATA (HTTPException): {e.detail} (auth.py) !!!")
        print("="*50 + "\n")
        # --- LOGLAMA BİTTİ ---
        raise e
    except Exception as e:
        # --- DETAYLI LOGLAMA EKLENDİ ---
        print(f"!!! ADIM 1 HATA (Genel Exception): {str(e)} (auth.py) !!!")
        print("="*50 + "\n")
        # --- LOGLAMA BİTTİ ---
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=safe_error_message(e, "Şifre belirlenirken bir hata oluştu")
        )
@router.post("/change-password", status_code=status.HTTP_200_OK)
def change_password(
    request: ChangePasswordRequest,
    current_user: UserInDB = Depends(get_current_user),
    db: BaseRepository = Depends(get_db_repository)
):
    """Giriş yapmış kullanıcının şifresini değiştirir."""
    auth_service.change_user_password(
        user=current_user,
        old_password=request.old_password,
        new_password=request.new_password,
        db=db
    )
    return {"message": "Şifre başarıyla değiştirildi."}