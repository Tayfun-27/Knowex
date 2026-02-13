# backend/app/api/v1/integrations.py

from fastapi import APIRouter, Depends, HTTPException, status, Query
from typing import List, Optional
from datetime import datetime
from app.dependencies import get_current_admin_user
from app.schemas.user import UserInDB
from app.schemas.integration import (
    ApiIntegration, ApiIntegrationCreate, ApiIntegrationUpdate,
    ApiIntegrationResponse, ApiCallRequest, ApiCallResponse,
    ApiTestResponse, AuthType, HTTPMethod
)
from app.services.api_integration_service import make_api_call, test_api_connection
from app.core.config import ENVIRONMENT, DEBUG
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
import base64
import json

def safe_error_message(e: Exception, default_message: str) -> str:
    """Production'da hassas bilgi sızıntısını önlemek için güvenli hata mesajı döndürür."""
    if ENVIRONMENT == "production" and not DEBUG:
        return default_message
    else:
        return f"{default_message}: {str(e)}"

router = APIRouter()

def encrypt_credential(value: str) -> str:
    """Credential'ı şifreler (basit base64 encoding - production'da daha güvenli yöntem kullanılmalı)"""
    if not value:
        return ""
    return base64.b64encode(value.encode()).decode()

def decrypt_credential(encrypted: str) -> str:
    """Credential'ı çözer"""
    if not encrypted:
        return ""
    try:
        return base64.b64decode(encrypted.encode()).decode()
    except:
        return encrypted  # Eğer decode edilemezse, zaten şifrelenmemiş olabilir

def integration_to_response(integration_data: dict, include_secrets: bool = False) -> ApiIntegrationResponse:
    """Firestore'dan gelen integration verisini response modeline çevirir"""
    integration_id = integration_data.get("id") or integration_data.get("__id__")
    
    # Şifreleri çöz (eğer şifrelenmişse)
    password = integration_data.get("password")
    bearer_token = integration_data.get("bearer_token")
    api_key = integration_data.get("api_key")
    
    if password and not include_secrets:
        password = decrypt_credential(password) if password else None
    if bearer_token and not include_secrets:
        bearer_token = decrypt_credential(bearer_token) if bearer_token else None
    if api_key and not include_secrets:
        api_key = decrypt_credential(api_key) if api_key else None
    
    return ApiIntegrationResponse(
        id=integration_id,
        tenant_id=integration_data.get("tenant_id", ""),
        name=integration_data.get("name", ""),
        description=integration_data.get("description"),
        api_url=integration_data.get("api_url", ""),
        auth_type=integration_data.get("auth_type", "basic"),
        username=integration_data.get("username"),
        has_password=bool(password),
        has_bearer_token=bool(bearer_token),
        has_api_key=bool(api_key),
        api_key_header=integration_data.get("api_key_header"),
        api_key_location=integration_data.get("api_key_location"),
        custom_headers=integration_data.get("custom_headers"),
        timeout_seconds=integration_data.get("timeout_seconds", 30),
        is_active=integration_data.get("is_active", True),
        created_at=integration_data.get("created_at", datetime.now()),
        updated_at=integration_data.get("updated_at", datetime.now()),
        last_test_at=integration_data.get("last_test_at"),
        last_test_status=integration_data.get("last_test_status")
    )

@router.get("/", response_model=List[ApiIntegrationResponse])
def list_integrations(
    admin_user: UserInDB = Depends(get_current_admin_user)
):
    """Tüm API entegrasyonlarını listeler"""
    try:
        db = firestore.Client()
        integrations_ref = db.collection("api_integrations")
        query = integrations_ref.where(filter=FieldFilter("tenant_id", "==", admin_user.tenant_id))
        docs = query.stream()
        
        integrations = []
        for doc in docs:
            data = doc.to_dict()
            data["id"] = doc.id
            integrations.append(integration_to_response(data))
        
        return integrations
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=safe_error_message(e, "Entegrasyonlar listelenirken bir hata oluştu")
        )

@router.get("/{integration_id}", response_model=ApiIntegrationResponse)
def get_integration(
    integration_id: str,
    admin_user: UserInDB = Depends(get_current_admin_user)
):
    """Belirli bir API entegrasyonunu getirir"""
    try:
        db = firestore.Client()
        doc = db.collection("api_integrations").document(integration_id).get()
        
        if not doc.exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Entegrasyon bulunamadı"
            )
        
        data = doc.to_dict()
        if data.get("tenant_id") != admin_user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Bu entegrasyona erişim yetkiniz yok"
            )
        
        data["id"] = doc.id
        return integration_to_response(data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=safe_error_message(e, "Entegrasyon getirilirken bir hata oluştu")
        )

@router.post("/", response_model=ApiIntegrationResponse, status_code=status.HTTP_201_CREATED)
def create_integration(
    integration: ApiIntegrationCreate,
    admin_user: UserInDB = Depends(get_current_admin_user)
):
    """Yeni API entegrasyonu oluşturur"""
    try:
        db = firestore.Client()
        
        # Şifreleri şifrele
        integration_data = integration.model_dump(exclude_none=True)
        
        if integration_data.get("password"):
            integration_data["password"] = encrypt_credential(integration_data["password"])
        if integration_data.get("bearer_token"):
            integration_data["bearer_token"] = encrypt_credential(integration_data["bearer_token"])
        if integration_data.get("api_key"):
            integration_data["api_key"] = encrypt_credential(integration_data["api_key"])
        
        # Metadata ekle
        integration_data["tenant_id"] = admin_user.tenant_id
        integration_data["created_at"] = datetime.now()
        integration_data["updated_at"] = datetime.now()
        integration_data["last_test_at"] = None
        integration_data["last_test_status"] = None
        
        # Firestore'a kaydet
        doc_ref = db.collection("api_integrations").document()
        doc_ref.set(integration_data)
        
        # Response oluştur
        integration_data["id"] = doc_ref.id
        return integration_to_response(integration_data)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=safe_error_message(e, "Entegrasyon oluşturulurken bir hata oluştu")
        )

@router.put("/{integration_id}", response_model=ApiIntegrationResponse)
def update_integration(
    integration_id: str,
    integration: ApiIntegrationUpdate,
    admin_user: UserInDB = Depends(get_current_admin_user)
):
    """API entegrasyonunu günceller"""
    try:
        db = firestore.Client()
        doc_ref = db.collection("api_integrations").document(integration_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Entegrasyon bulunamadı"
            )
        
        existing_data = doc.to_dict()
        if existing_data.get("tenant_id") != admin_user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Bu entegrasyona erişim yetkiniz yok"
            )
        
        # Güncelleme verilerini hazırla
        update_data = integration.model_dump(exclude_none=True)
        
        # Şifreleri şifrele (eğer yeni değerler verilmişse)
        if "password" in update_data and update_data["password"]:
            update_data["password"] = encrypt_credential(update_data["password"])
        elif "password" in update_data and update_data["password"] is None:
            # None ise mevcut değeri koru
            del update_data["password"]
        
        if "bearer_token" in update_data and update_data["bearer_token"]:
            update_data["bearer_token"] = encrypt_credential(update_data["bearer_token"])
        elif "bearer_token" in update_data and update_data["bearer_token"] is None:
            del update_data["bearer_token"]
        
        if "api_key" in update_data and update_data["api_key"]:
            update_data["api_key"] = encrypt_credential(update_data["api_key"])
        elif "api_key" in update_data and update_data["api_key"] is None:
            del update_data["api_key"]
        
        # Metadata güncelle
        update_data["updated_at"] = datetime.now()
        
        # Firestore'da güncelle
        doc_ref.update(update_data)
        
        # Güncellenmiş veriyi getir
        updated_doc = doc_ref.get()
        updated_data = updated_doc.to_dict()
        updated_data["id"] = updated_doc.id
        return integration_to_response(updated_data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=safe_error_message(e, "Entegrasyon güncellenirken bir hata oluştu")
        )

@router.delete("/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_integration(
    integration_id: str,
    admin_user: UserInDB = Depends(get_current_admin_user)
):
    """API entegrasyonunu siler"""
    try:
        db = firestore.Client()
        doc = db.collection("api_integrations").document(integration_id).get()
        
        if not doc.exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Entegrasyon bulunamadı"
            )
        
        data = doc.to_dict()
        if data.get("tenant_id") != admin_user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Bu entegrasyona erişim yetkiniz yok"
            )
        
        db.collection("api_integrations").document(integration_id).delete()
        return None
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=safe_error_message(e, "Entegrasyon silinirken bir hata oluştu")
        )

@router.post("/{integration_id}/test", response_model=ApiTestResponse)
def test_integration(
    integration_id: str,
    test_endpoint: Optional[str] = Query(None, description="Test endpoint (opsiyonel, varsayılan: /health)"),
    admin_user: UserInDB = Depends(get_current_admin_user)
):
    """API entegrasyonunu test eder"""
    try:
        db = firestore.Client()
        doc = db.collection("api_integrations").document(integration_id).get()
        
        if not doc.exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Entegrasyon bulunamadı"
            )
        
        data = doc.to_dict()
        if data.get("tenant_id") != admin_user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Bu entegrasyona erişim yetkiniz yok"
            )
        
        if not data.get("is_active"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Entegrasyon aktif değil"
            )
        
        # Şifreleri çöz
        password = decrypt_credential(data.get("password", "")) if data.get("password") else None
        bearer_token = decrypt_credential(data.get("bearer_token", "")) if data.get("bearer_token") else None
        api_key = decrypt_credential(data.get("api_key", "")) if data.get("api_key") else None
        
        # Test yap
        test_result = test_api_connection(
            api_url=data.get("api_url", ""),
            auth_type=AuthType(data.get("auth_type", "basic")),
            username=data.get("username"),
            password=password,
            bearer_token=bearer_token,
            api_key=api_key,
            api_key_header=data.get("api_key_header"),
            api_key_location=data.get("api_key_location"),
            custom_headers=data.get("custom_headers"),
            test_endpoint=test_endpoint,
            timeout=data.get("timeout_seconds", 30)
        )
        
        # Test sonucunu kaydet
        db.collection("api_integrations").document(integration_id).update({
            "last_test_at": datetime.now(),
            "last_test_status": "success" if test_result.success else "error"
        })
        
        return test_result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=safe_error_message(e, "Entegrasyon test edilirken bir hata oluştu")
        )

@router.post("/{integration_id}/call", response_model=ApiCallResponse)
def make_integration_call(
    integration_id: str,
    call_request: ApiCallRequest,
    admin_user: UserInDB = Depends(get_current_admin_user)
):
    """API entegrasyonu üzerinden API çağrısı yapar"""
    try:
        # Integration ID kontrolü
        if call_request.integration_id != integration_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Integration ID uyuşmuyor"
            )
        
        db = firestore.Client()
        doc = db.collection("api_integrations").document(integration_id).get()
        
        if not doc.exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Entegrasyon bulunamadı"
            )
        
        data = doc.to_dict()
        if data.get("tenant_id") != admin_user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Bu entegrasyona erişim yetkiniz yok"
            )
        
        if not data.get("is_active"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Entegrasyon aktif değil"
            )
        
        # Şifreleri çöz
        password = decrypt_credential(data.get("password", "")) if data.get("password") else None
        bearer_token = decrypt_credential(data.get("bearer_token", "")) if data.get("bearer_token") else None
        api_key = decrypt_credential(data.get("api_key", "")) if data.get("api_key") else None
        
        # API çağrısı yap
        response = make_api_call(
            api_url=data.get("api_url", ""),
            endpoint=call_request.endpoint,
            method=call_request.method,
            auth_type=AuthType(data.get("auth_type", "basic")),
            username=data.get("username"),
            password=password,
            bearer_token=bearer_token,
            api_key=api_key,
            api_key_header=data.get("api_key_header"),
            api_key_location=data.get("api_key_location"),
            custom_headers={**(data.get("custom_headers") or {}), **(call_request.headers or {})},
            params=call_request.params,
            body=call_request.body,
            timeout=call_request.timeout or data.get("timeout_seconds", 30)
        )
        
        return response
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=safe_error_message(e, "API çağrısı yapılırken bir hata oluştu")
        )

