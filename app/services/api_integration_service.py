# backend/app/services/api_integration_service.py

import requests
import time
from typing import Dict, Any, Optional
from app.schemas.integration import (
    ApiCallRequest, ApiCallResponse, ApiTestResponse,
    AuthType, HTTPMethod
)

def make_api_call(
    api_url: str,
    endpoint: str,
    method: HTTPMethod,
    auth_type: AuthType,
    username: Optional[str] = None,
    password: Optional[str] = None,
    bearer_token: Optional[str] = None,
    api_key: Optional[str] = None,
    api_key_header: Optional[str] = "X-API-Key",
    api_key_location: Optional[str] = "header",
    custom_headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
    timeout: int = 30
) -> ApiCallResponse:
    """API çağrısı yapar"""
    
    start_time = time.time()
    
    try:
        # Base URL ve endpoint'i birleştir
        if endpoint.startswith("http"):
            full_url = endpoint
        else:
            # Base URL'den trailing slash'i temizle, endpoint'ten başındaki slash'i temizle
            base_url = api_url.rstrip('/')
            endpoint_clean = endpoint.lstrip('/')
            full_url = f"{base_url}/{endpoint_clean}"
        
        # Header'ları hazırla
        headers = {}
        
        # Custom header'ları ekle
        if custom_headers:
            headers.update(custom_headers)
        
        # Authentication header'larını ekle
        if auth_type == AuthType.BASIC:
            if username and password:
                from requests.auth import HTTPBasicAuth
                auth = HTTPBasicAuth(username, password)
            else:
                auth = None
        elif auth_type == AuthType.BEARER:
            if bearer_token:
                headers["Authorization"] = f"Bearer {bearer_token}"
            auth = None
        elif auth_type == AuthType.API_KEY:
            if api_key:
                if api_key_location == "header":
                    headers[api_key_header or "X-API-Key"] = api_key
                elif api_key_location == "query":
                    # Query parametrelerine eklenecek
                    if params is None:
                        params = {}
                    params[api_key_header or "api_key"] = api_key
            auth = None
        else:
            auth = None
        
        # Content-Type header'ı ekle (body varsa)
        if body and method in [HTTPMethod.POST, HTTPMethod.PUT, HTTPMethod.PATCH]:
            if "Content-Type" not in headers:
                headers["Content-Type"] = "application/json"
        
        # Request yap
        response = requests.request(
            method=method.value,
            url=full_url,
            headers=headers,
            params=params,
            json=body if body else None,
            auth=auth,
            timeout=timeout
        )
        
        response_time_ms = (time.time() - start_time) * 1000
        
        # Response'u parse et
        try:
            response_data = response.json()
        except:
            response_data = response.text
        
        return ApiCallResponse(
            success=response.status_code < 400,
            status_code=response.status_code,
            headers=dict(response.headers),
            data=response_data,
            error=None if response.status_code < 400 else f"HTTP {response.status_code}: {response.text[:500]}",
            response_time_ms=response_time_ms
        )
        
    except requests.exceptions.Timeout:
        response_time_ms = (time.time() - start_time) * 1000
        return ApiCallResponse(
            success=False,
            status_code=None,
            headers=None,
            data=None,
            error=f"Request timeout after {timeout} seconds",
            response_time_ms=response_time_ms
        )
    except requests.exceptions.ConnectionError as e:
        response_time_ms = (time.time() - start_time) * 1000
        return ApiCallResponse(
            success=False,
            status_code=None,
            headers=None,
            data=None,
            error=f"Connection error: {str(e)}",
            response_time_ms=response_time_ms
        )
    except Exception as e:
        response_time_ms = (time.time() - start_time) * 1000
        return ApiCallResponse(
            success=False,
            status_code=None,
            headers=None,
            data=None,
            error=f"Error: {str(e)}",
            response_time_ms=response_time_ms
        )

def test_api_connection(
    api_url: str,
    auth_type: AuthType,
    username: Optional[str] = None,
    password: Optional[str] = None,
    bearer_token: Optional[str] = None,
    api_key: Optional[str] = None,
    api_key_header: Optional[str] = "X-API-Key",
    api_key_location: Optional[str] = "header",
    custom_headers: Optional[Dict[str, str]] = None,
    test_endpoint: Optional[str] = None,
    timeout: int = 30
) -> ApiTestResponse:
    """API bağlantısını test eder"""
    
    start_time = time.time()
    
    try:
        # Test endpoint'i belirle
        if test_endpoint:
            endpoint = test_endpoint
        else:
            # Varsayılan test endpoint'leri
            endpoint = "/health"  # Çoğu API'de health check endpoint'i var
        
        # API çağrısı yap
        response = make_api_call(
            api_url=api_url,
            endpoint=endpoint,
            method=HTTPMethod.GET,
            auth_type=auth_type,
            username=username,
            password=password,
            bearer_token=bearer_token,
            api_key=api_key,
            api_key_header=api_key_header,
            api_key_location=api_key_location,
            custom_headers=custom_headers,
            timeout=timeout
        )
        
        response_time_ms = (time.time() - start_time) * 1000
        
        if response.success:
            return ApiTestResponse(
                success=True,
                status_code=response.status_code,
                message=f"Bağlantı başarılı (HTTP {response.status_code})",
                response_time_ms=response_time_ms,
                error=None
            )
        else:
            return ApiTestResponse(
                success=False,
                status_code=response.status_code,
                message=f"Bağlantı hatası (HTTP {response.status_code})",
                response_time_ms=response_time_ms,
                error=response.error
            )
            
    except Exception as e:
        response_time_ms = (time.time() - start_time) * 1000
        return ApiTestResponse(
            success=False,
            status_code=None,
            message="Bağlantı testi başarısız",
            response_time_ms=response_time_ms,
            error=str(e)
        )

