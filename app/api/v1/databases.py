# backend/app/api/v1/databases.py

from fastapi import APIRouter, Depends, HTTPException, status
from typing import List, Optional
from datetime import datetime
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.schemas.database import (
    DatabaseConnection,
    DatabaseConnectionCreate,
    DatabaseConnectionUpdate,
    DatabaseConnectionTest
)
from app.schemas.user import UserInDB
from app.dependencies import get_current_user, get_current_admin_user
from app.database_connectors import (
    PostgreSQLConnector,
    MongoDBConnector,
    MySQLConnector
)

router = APIRouter()

def safe_error_message(e: Exception, default_message: str) -> str:
    """
    Production'da hassas bilgi sızıntısını önlemek için güvenli hata mesajı döndürür.
    """
    from app.core.config import ENVIRONMENT, DEBUG
    if ENVIRONMENT == "production" and not DEBUG:
        return default_message
    else:
        return f"{default_message}: {str(e)}"

def get_database_connector(db_type: str):
    """Veritabanı tipine göre connector döndür"""
    if db_type == "postgresql":
        return PostgreSQLConnector()
    elif db_type == "mongodb":
        return MongoDBConnector()
    elif db_type == "mysql":
        return MySQLConnector()
    else:
        raise ValueError(f"Desteklenmeyen veritabanı tipi: {db_type}")

def get_database_collection():
    """Firestore collection referansı"""
    db = firestore.Client()
    return db.collection("database_connections")

@router.get("/connections", response_model=List[DatabaseConnection])
def get_database_connections(
    current_user: UserInDB = Depends(get_current_user)
):
    """Kullanıcının tenant'ına ait veritabanı bağlantılarını listele"""
    try:
        col = get_database_collection()
        query = col.where(filter=FieldFilter("tenant_id", "==", current_user.tenant_id)).where(filter=FieldFilter("is_active", "==", True))
        
        connections = []
        for doc in query.stream():
            data = doc.to_dict()
            data["id"] = doc.id
            connections.append(DatabaseConnection(**data))
        
        return connections
    except HTTPException:
        raise
    except Exception as e:
        print(f"Veritabanı bağlantıları getirilirken hata: {e}")
        raise HTTPException(status_code=500, detail=safe_error_message(e, "Veritabanı bağlantıları getirilirken bir hata oluştu"))

@router.get("/connections/{connection_id}", response_model=DatabaseConnection)
def get_database_connection(
    connection_id: str,
    current_user: UserInDB = Depends(get_current_user)
):
    """Tek bir veritabanı bağlantısını getir"""
    try:
        col = get_database_collection()
        doc = col.document(connection_id).get()
        
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Veritabanı bağlantısı bulunamadı")
        
        data = doc.to_dict()
        if data.get("tenant_id") != current_user.tenant_id:
            raise HTTPException(status_code=403, detail="Bu bağlantıya erişim yetkiniz yok")
        
        data["id"] = doc.id
        return DatabaseConnection(**data)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Veritabanı bağlantısı getirilirken hata: {e}")
        raise HTTPException(status_code=500, detail=safe_error_message(e, "Veritabanı bağlantısı getirilirken bir hata oluştu"))

@router.post("/connections", response_model=DatabaseConnection)
def create_database_connection(
    connection: DatabaseConnectionCreate,
    current_user: UserInDB = Depends(get_current_admin_user),  # Sadece admin
):
    """Yeni veritabanı bağlantısı oluştur"""
    try:
        # Bağlantıyı test et
        connector = get_database_connector(connection.type)
        try:
            if not connector.connect(connection.connection_string):
                raise HTTPException(
                    status_code=400,
                    detail="Veritabanı bağlantısı başarısız. Lütfen connection string'i kontrol edin."
                )
        except Exception as conn_error:
            # Bağlantı hatasının detayını göster
            error_detail = str(conn_error)
            raise HTTPException(
                status_code=400,
                detail=f"Veritabanı bağlantısı başarısız: {error_detail}. Lütfen connection string'i kontrol edin."
            )
        
        # Şema önizlemesi al
        try:
            schema = connector.get_schema()
        except Exception as schema_error:
            connector.close()
            raise HTTPException(
                status_code=400,
                detail=f"Veritabanı şeması alınamadı: {str(schema_error)}"
            )
        connector.close()
        
        # Firestore'a kaydet
        col = get_database_collection()
        doc_data = {
            "name": connection.name,
            "type": connection.type,
            "connection_string": connection.connection_string,  # Gerçek uygulamada şifreleme gerekir
            "description": connection.description,
            "tenant_id": current_user.tenant_id,
            "created_by": current_user.id,
            "is_active": True,
            "created_at": datetime.now().isoformat()
        }
        
        doc_ref = col.document()
        doc_ref.set(doc_data)
        doc_data["id"] = doc_ref.id
        
        return DatabaseConnection(**doc_data)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Veritabanı bağlantısı oluşturulurken hata: {e}")
        raise HTTPException(status_code=500, detail=safe_error_message(e, "Veritabanı bağlantısı oluşturulurken bir hata oluştu"))

@router.put("/connections/{connection_id}", response_model=DatabaseConnection)
def update_database_connection(
    connection_id: str,
    connection_update: DatabaseConnectionUpdate,
    current_user: UserInDB = Depends(get_current_admin_user),  # Sadece admin
):
    """Veritabanı bağlantısını güncelle"""
    try:
        col = get_database_collection()
        doc = col.document(connection_id).get()
        
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Veritabanı bağlantısı bulunamadı")
        
        data = doc.to_dict()
        if data.get("tenant_id") != current_user.tenant_id:
            raise HTTPException(status_code=403, detail="Bu bağlantıya erişim yetkiniz yok")
        
        # Güncelleme verilerini hazırla
        update_data = connection_update.model_dump(exclude_none=True)
        
        # Connection string değiştiyse test et
        if "connection_string" in update_data:
            connector = get_database_connector(data["type"])
            try:
                if not connector.connect(update_data["connection_string"]):
                    raise HTTPException(
                        status_code=400,
                        detail="Yeni connection string ile bağlantı başarısız."
                    )
            except Exception as conn_error:
                error_detail = str(conn_error)
                raise HTTPException(
                    status_code=400,
                    detail=f"Yeni connection string ile bağlantı başarısız: {error_detail}"
                )
            connector.close()
        
        # Firestore'u güncelle
        doc.reference.update(update_data)
        
        # Güncellenmiş veriyi getir
        updated_doc = col.document(connection_id).get()
        updated_data = updated_doc.to_dict()
        updated_data["id"] = connection_id
        
        return DatabaseConnection(**updated_data)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Veritabanı bağlantısı güncellenirken hata: {e}")
        raise HTTPException(status_code=500, detail=safe_error_message(e, "Veritabanı bağlantısı güncellenirken bir hata oluştu"))

@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_database_connection(
    connection_id: str,
    current_user: UserInDB = Depends(get_current_admin_user),  # Sadece admin
):
    """Veritabanı bağlantısını sil (soft delete)"""
    try:
        col = get_database_collection()
        doc = col.document(connection_id).get()
        
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Veritabanı bağlantısı bulunamadı")
        
        data = doc.to_dict()
        if data.get("tenant_id") != current_user.tenant_id:
            raise HTTPException(status_code=403, detail="Bu bağlantıya erişim yetkiniz yok")
        
        # Soft delete: is_active = False
        doc.reference.update({"is_active": False})
        return
    except HTTPException:
        raise
    except Exception as e:
        print(f"Veritabanı bağlantısı silinirken hata: {e}")
        raise HTTPException(status_code=500, detail=safe_error_message(e, "Veritabanı bağlantısı silinirken bir hata oluştu"))

@router.post("/connections/{connection_id}/test", response_model=DatabaseConnectionTest)
def test_database_connection(
    connection_id: str,
    current_user: UserInDB = Depends(get_current_user),
):
    """Veritabanı bağlantısını test et"""
    try:
        col = get_database_collection()
        doc = col.document(connection_id).get()
        
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Veritabanı bağlantısı bulunamadı")
        
        data = doc.to_dict()
        if data.get("tenant_id") != current_user.tenant_id:
            raise HTTPException(status_code=403, detail="Bu bağlantıya erişim yetkiniz yok")
        
        # Bağlantıyı test et
        connector = get_database_connector(data["type"])
        try:
            success = connector.connect(data["connection_string"])
            
            if not success:
                return DatabaseConnectionTest(
                    success=False,
                    message="Bağlantı başarısız. Connection string'i kontrol edin."
                )
            
            # Şema önizlemesi al
            try:
                schema = connector.get_schema()
            except Exception as schema_error:
                connector.close()
                return DatabaseConnectionTest(
                    success=False,
                    message=f"Bağlantı başarılı ancak şema alınamadı: {str(schema_error)}"
                )
            
            connector.close()
            
            return DatabaseConnectionTest(
                success=True,
                message="Bağlantı başarılı!",
                schema_preview=schema
            )
        except Exception as conn_error:
            error_detail = str(conn_error)
            return DatabaseConnectionTest(
                success=False,
                message=f"Bağlantı hatası: {error_detail}"
            )
    except HTTPException:
        raise
    except Exception as e:
        print(f"Veritabanı bağlantı testi hatası: {e}")
        return DatabaseConnectionTest(
            success=False,
            message=f"Test sırasında hata: {str(e)}"
        )

