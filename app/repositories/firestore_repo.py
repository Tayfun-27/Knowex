# backend/app/repositories/firestore_repo.py

from __future__ import annotations
from typing import Optional, List, Dict, Any, Set
from datetime import datetime

from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector
from google.cloud.firestore_v1.base_query import DistanceMeasure, FieldFilter
from google.cloud.firestore_v1.field_path import FieldPath

from app.repositories.base import BaseRepository
from app.schemas.user import UserCreate, UserInDB, UserOut
from app.schemas.folder import FolderCreate, FolderOut
from app.schemas.file import FileCreate, FileOut
from app.schemas.tenant import TenantOut
from app.schemas.mention import MentionableItem
from app.schemas.role import RoleCreate, RoleOut, RolePermissionUpdate
from app.schemas.chat import ChatMessage, ChatSession


class FirestoreRepository(BaseRepository):
    def __init__(self):
        self.db = firestore.Client()
        self.users_collection = self.db.collection("users")
        self.folders_collection = self.db.collection("folders")
        self.files_collection = self.db.collection("files")
        self.tenants_collection = self.db.collection("tenants")
        self.chunks_collection = self.db.collection("text_chunks")
        self.roles_collection = self.db.collection("roles")
        self.chats_collection = self.db.collection("chats")

    # ------------------ USERS ------------------
    def get_user_by_email(self, email: str) -> Optional[UserInDB]:
        query = (
            self.users_collection.where(filter=FieldFilter("email", "==", email))
            .limit(1)
            .stream()
        )
        for doc in query:
            user_data = doc.to_dict() or {}
            user_data["id"] = doc.id
            # Geriye dönük uyumluluk: roles yoksa role'den türet
            if "roles" not in user_data or not user_data.get("roles"):
                user_data["roles"] = [user_data.get("role", "User")]
            return UserInDB(**user_data)
        return None

    def create_user(self, user_create: UserCreate, hashed_password: Optional[str]) -> UserInDB:
        user_data = user_create.model_dump(exclude={"password"})
        user_data["hashed_password"] = hashed_password
        doc_ref = self.users_collection.document()
        doc_ref.set(user_data)
        return UserInDB(id=doc_ref.id, **user_data)

    def get_user_by_id(self, user_id: str) -> Optional[UserOut]:
        doc = self.users_collection.document(user_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict() or {}
        data["id"] = doc.id
        # Geriye dönük uyumluluk: roles yoksa role'den türet
        if "roles" not in data or not data.get("roles"):
            data["roles"] = [data.get("role", "User")]
        return UserOut(**data)

    def get_users_by_tenant(self, tenant_id: str) -> List[UserOut]:
        query = self.users_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))
        users = []
        for doc in query.stream():
            data = doc.to_dict() or {}
            data["id"] = doc.id
            # Geriye dönük uyumluluk: roles yoksa role'den türet
            if "roles" not in data or not data.get("roles"):
                data["roles"] = [data.get("role", "User")]
            users.append(UserOut(**data))
        return users

    def update_user_role(self, tenant_id: str, user_id: str, new_role: str) -> bool:
        """Geriye dönük uyumluluk için korundu. Tek rol güncellemesi için."""
        return self.update_user_roles(tenant_id, user_id, [new_role], new_role)
    
    def update_user_roles(self, tenant_id: str, user_id: str, new_roles: List[str], new_role: str) -> bool:
        """Çoklu rol güncellemesi için yeni metod."""
        try:
            doc_ref = self.users_collection.document(user_id)
            doc = doc_ref.get()
            if not doc.exists or (doc.to_dict() or {}).get("tenant_id") != tenant_id:
                return False
            doc_ref.update({"roles": new_roles, "role": new_role})
            return True
        except Exception as e:
            print(f"Rol güncellenirken hata: {e}")
            return False

    def delete_user(self, tenant_id: str, user_id: str) -> bool:
        try:
            doc_ref = self.users_collection.document(user_id)
            doc = doc_ref.get()
            if not doc.exists or (doc.to_dict() or {}).get("tenant_id") != tenant_id:
                return False
            doc_ref.delete()
            return True
        except Exception as e:
            print(f"Kullanıcı silinirken hata: {e}")
            return False

    # ------------------ FOLDERS ------------------
    def create_folder(self, folder_data: FolderCreate, owner_id: str, tenant_id: str) -> FolderOut:
        folder_dict = folder_data.model_dump()
        folder_dict["owner_id"] = owner_id
        folder_dict["tenant_id"] = tenant_id
        doc_ref = self.folders_collection.document()
        doc_ref.set(folder_dict)
        return FolderOut(id=doc_ref.id, **folder_dict)

    def get_folders_by_parent(self, tenant_id: str, parent_id: Optional[str]) -> List[FolderOut]:
        query = (
            self.folders_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("parent_id", "==", parent_id))
        )
        return [FolderOut(id=doc.id, **(doc.to_dict() or {})) for doc in query.stream()]

    def get_folders_by_ids(self, tenant_id: str, folder_ids: List[str]) -> List[FolderOut]:
        if not folder_ids:
            return []
        query = self.folders_collection.where(
            filter=FieldFilter(FieldPath.document_id(), "in", folder_ids[:10])
        )
        results: List[FolderOut] = []
        for doc in query.stream():
            data = doc.to_dict() or {}
            if data.get("tenant_id") == tenant_id:
                results.append(FolderOut(id=doc.id, **data))
        return results

    def delete_folder(self, tenant_id: str, folder_id: str) -> bool:
        try:
            doc_ref = self.folders_collection.document(folder_id)
            doc = doc_ref.get()
            if not doc.exists or (doc.to_dict() or {}).get("tenant_id") != tenant_id:
                return False
            doc_ref.delete()
            return True
        except Exception as e:
            print(f"Klasör kaydı silinirken hata: {e}")
            return False

    # ------------------ FILES ------------------
    def create_file_record(self, file_data: FileCreate) -> FileOut:
        file_dict = file_data.model_dump()
        doc_ref = self.files_collection.document()
        doc_ref.set(file_dict)
        return FileOut(id=doc_ref.id, **file_dict)

    def get_files_by_folder(self, tenant_id: str, folder_id: Optional[str]) -> List[FileOut]:
        query = (
            self.files_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("folder_id", "==", folder_id))
            .order_by("created_at", direction=firestore.Query.DESCENDING)
        )
        return [FileOut(id=doc.id, **(doc.to_dict() or {})) for doc in query.stream()]

    def get_files_by_ids(self, tenant_id: str, file_ids: List[str]) -> List[FileOut]:
        if not file_ids:
            return []
        query = self.files_collection.where(
            filter=FieldFilter(FieldPath.document_id(), "in", file_ids[:10])
        )
        results: List[FileOut] = []
        for doc in query.stream():
            data = doc.to_dict() or {}
            if data.get("tenant_id") == tenant_id:
                results.append(FileOut(id=doc.id, **data))
        return results

    def get_file_by_id(self, tenant_id: str, file_id: str) -> Optional[FileOut]:
        doc = self.files_collection.document(file_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict() or {}
        if data.get("tenant_id") != tenant_id:
            return None
        return FileOut(id=doc.id, **data)

    def delete_file_record(self, tenant_id: str, file_id: str) -> bool:
        try:
            doc_ref = self.files_collection.document(file_id)
            doc = doc_ref.get()
            if not doc.exists or (doc.to_dict() or {}).get("tenant_id") != tenant_id:
                return False
            doc_ref.delete()
            return True
        except Exception as e:
            print(f"Dosya kaydı silinirken hata: {e}")
            return False

    def move_file(self, tenant_id: str, file_id: str, new_folder_id: Optional[str], user: UserInDB) -> bool:
        file_record = self.get_file_by_id(tenant_id, file_id)
        if not file_record:
            return False

        if user.role != "Admin" and file_record.owner_id != user.id:
            print(
                f"YETKİSİZ TAŞIMA DENEMESİ: Kullanıcı {user.email} (ID: {user.id}), {file_record.owner_id} ID'li sahibin dosyasını taşımaya çalıştı."
            )
            return False

        self.files_collection.document(file_id).update({"folder_id": new_folder_id})
        return True

    def check_file_exists(self, tenant_id: str, folder_id: Optional[str], file_name: str) -> bool:
        query = (
            self.files_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("folder_id", "==", folder_id))
            .where(filter=FieldFilter("name", "==", file_name))
            .limit(1)
        )
        return any(query.stream())

    def rename_file(self, tenant_id: str, file_id: str, new_name: str) -> bool:
        try:
            doc_ref = self.files_collection.document(file_id)
            doc = doc_ref.get()
            if not doc.exists or (doc.to_dict() or {}).get("tenant_id") != tenant_id:
                return False

            folder_id = (doc.to_dict() or {}).get("folder_id")
            if self.check_file_exists(tenant_id, folder_id, new_name):
                raise ValueError(f"'{new_name}' adında bir dosya zaten mevcut.")

            doc_ref.update({"name": new_name})
            return True
        except Exception as e:
            print(f"Dosya yeniden adlandırılırken hata: {e}")
            raise

    # ------------------ TENANTS ------------------
    def get_tenant_by_id(self, tenant_id: str) -> Optional[TenantOut]:
        doc = self.tenants_collection.document(tenant_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict() or {}
        return TenantOut(id=doc.id, **data)

    # ------------------ LISTS ------------------
    def get_all_files_for_tenant(self, tenant_id: str) -> List[FileOut]:
        query = self.files_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))
        return [FileOut(id=doc.id, **(doc.to_dict() or {})) for doc in query.stream()]

    def get_all_folders_for_tenant(self, tenant_id: str) -> List[FolderOut]:
        query = self.folders_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))
        return [FolderOut(id=doc.id, **(doc.to_dict() or {})) for doc in query.stream()]

    def get_all_file_ids_in_folder_recursive(self, tenant_id: str, folder_id: str, user: UserInDB) -> List[str]:
        if user.role == "Admin":
            all_file_ids: List[str] = []
            folder_queue = [folder_id]
            while folder_queue:
                current_folder_id = folder_queue.pop(0)

                files_query = (
                    self.files_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))
                    .where(filter=FieldFilter("folder_id", "==", current_folder_id))
                )
                for doc in files_query.stream():
                    all_file_ids.append(doc.id)

                subfolders_query = (
                    self.folders_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))
                    .where(filter=FieldFilter("parent_id", "==", current_folder_id))
                )
                for doc in subfolders_query.stream():
                    folder_queue.append(doc.id)
            return all_file_ids

        user_role = self.get_role_by_name(tenant_id, user.role)
        if not user_role:
            return []

        allowed_files_set = set(user_role.allowed_files or [])
        allowed_folders_set = set(user_role.allowed_folders or [])

        all_files_in_hierarchy: List[Dict[str, Any]] = []
        folder_queue = [folder_id]
        while folder_queue:
            current_folder_id = folder_queue.pop(0)

            files_query = (
                self.files_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))
                .where(filter=FieldFilter("folder_id", "==", current_folder_id))
            )
            for doc in files_query.stream():
                file_data = doc.to_dict() or {}
                all_files_in_hierarchy.append(
                    {
                        "id": doc.id,
                        "folder_id": file_data.get("folder_id"),
                    }
                )

            subfolders_query = (
                self.folders_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))
                .where(filter=FieldFilter("parent_id", "==", current_folder_id))
            )
            for doc in subfolders_query.stream():
                folder_queue.append(doc.id)

        visible_file_ids: List[str] = []
        for info in all_files_in_hierarchy:
            if info["id"] in allowed_files_set or info["folder_id"] in allowed_folders_set:
                visible_file_ids.append(info["id"])
        return visible_file_ids

    # ------------------ TEXT CHUNKS / VECTORS ------------------
    def add_text_chunks_batch(self, chunks_data: List[Dict[str, Any]]):
        if not chunks_data:
            return
        batch = self.db.batch()
        for chunk_data in chunks_data:
            if "embedding" in chunk_data and chunk_data["embedding"] is not None:
                chunk_data["embedding"] = Vector(chunk_data["embedding"])
            doc_ref = self.chunks_collection.document()
            batch.set(doc_ref, chunk_data)
        batch.commit()

    def find_similar_chunks(
        self,
        tenant_id: str,
        query_vector: List[float],
        limit: int,
        file_id_filter: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:

        query = self.chunks_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))

        if file_id_filter:
            file_id_list = list(file_id_filter)
            # Firestore 'in' operatörü güvenli limit: 10
            if len(file_id_list) > 10:
                print(f"UYARI: Firestore 'in' limiti 10. {len(file_id_list)} adet filtreden ilk 10 kullanılacak.")
                file_id_list = file_id_list[:10]

            if not file_id_list:
                return []
            query = query.where(filter=FieldFilter("file_id", "in", file_id_list))

        vector_query = query.find_nearest(
            vector_field="embedding",
            query_vector=Vector(query_vector),
            limit=limit,
            distance_measure=DistanceMeasure.COSINE,
        )

        results: List[Dict[str, Any]] = []

        # SDK sürüm farklarına dayanıklı iterasyon:
        # - Bazı sürümlerde vector_query.stream() gerekir ve item.document + item.distance gelir.
        # - Bazılarında doğrudan DocumentSnapshot iterate edilir ve distance farklı addadır.
        try:
            iterator = vector_query.stream()
        except Exception:
            iterator = iter(vector_query)

        for item in iterator:
            # Wrapper ise item.document; değilse item zaten DocumentSnapshot'tır
            doc = getattr(item, "document", item)

            # Mesafe metriklerini farklı adlarla yakala
            distance = getattr(item, "distance", None)
            if distance is None:
                distance = getattr(item, "vector_distance", None)

            data = doc.to_dict() if hasattr(doc, "to_dict") else None
            if not data:
                continue

            data["id"] = getattr(doc, "id", None)

            # cosine distance -> similarity; distance yoksa 0.0
            try:
                sim = 1.0 - float(distance) if distance is not None else 0.0
            except Exception:
                sim = 0.0
            data["similarity_score"] = float(sim)

            results.append(data)

        return results

    def delete_chunks_for_file(self, tenant_id: str, file_id: str):
        try:
            query = (
                self.chunks_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))
                .where(filter=FieldFilter("file_id", "==", file_id))
            )
            docs_to_delete = list(query.stream())
            if not docs_to_delete:
                return
            batch = self.db.batch()
            for doc in docs_to_delete:
                batch.delete(doc.reference)
            batch.commit()
            print(f"'{file_id}' dosyasına ait {len(docs_to_delete)} chunk silindi.")
        except Exception as e:
            print(f"'{file_id}' dosyasına ait chunk'lar silinirken hata: {e}")

    # ------------------ ROLES ------------------
    def create_role(self, role_data: RoleCreate) -> RoleOut:
        role_dict = role_data.model_dump()
        doc_ref = self.roles_collection.document()
        doc_ref.set(role_dict)
        return RoleOut(id=doc_ref.id, **role_dict)

    def get_roles_by_tenant(self, tenant_id: str) -> List[RoleOut]:
        query = self.roles_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))
        return [RoleOut(id=doc.id, **(doc.to_dict() or {})) for doc in query.stream()]

    def update_role_permissions(self, tenant_id: str, role_id: str, permissions: RolePermissionUpdate) -> bool:
        try:
            doc_ref = self.roles_collection.document(role_id)
            doc = doc_ref.get()
            if not doc.exists or (doc.to_dict() or {}).get("tenant_id") != tenant_id:
                return False
            doc_ref.update(permissions.model_dump())
            return True
        except Exception as e:
            print(f"Rol izinleri güncellenirken hata: {e}")
            return False

    def is_role_assigned_to_users(self, tenant_id: str, role_name: str) -> bool:
        query = (
            self.users_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("role", "==", role_name))
            .limit(1)
        )
        return any(query.stream())

    def delete_role(self, tenant_id: str, role_id: str) -> bool:
        try:
            doc_ref = self.roles_collection.document(role_id)
            doc = doc_ref.get()
            if not doc.exists or (doc.to_dict() or {}).get("tenant_id") != tenant_id:
                return False
            doc_ref.delete()
            return True
        except Exception as e:
            print(f"Rol silinirken hata: {e}")
            return False

    def get_role_by_id(self, tenant_id: str, role_id: str) -> Optional[RoleOut]:
        try:
            doc = self.roles_collection.document(role_id).get()
            if not doc.exists or (doc.to_dict() or {}).get("tenant_id") != tenant_id:
                return None
            data = doc.to_dict() or {}
            return RoleOut(id=doc.id, **data)
        except Exception as e:
            print(f"Rol getirilirken hata: {e}")
            return None

    def get_role_by_name(self, tenant_id: str, role_name: str) -> Optional[RoleOut]:
        try:
            query = (
                self.roles_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))
                .where(filter=FieldFilter("name", "==", role_name))
                .limit(1)
            )
            docs = list(query.stream())
            if not docs:
                print(f"'{role_name}' adında rol bulunamadı.")
                return None
            data = docs[0].to_dict() or {}
            return RoleOut(id=docs[0].id, **data)
        except Exception as e:
            print(f"Rol ismine göre getirilirken hata: {e}")
            return None

    # ------------------ CHATS ------------------
    def get_chat_sessions(self, user_id: str, tenant_id: str) -> List[ChatSession]:
        query = (
            self.chats_collection.where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("user_id", "==", user_id))
            .order_by("created_at", direction=firestore.Query.DESCENDING)
        )
        return [ChatSession(id=doc.id, **(doc.to_dict() or {})) for doc in query.stream()]

    def get_chat_session_by_id(self, chat_id: str, tenant_id: str) -> Optional[ChatSession]:
        doc = self.chats_collection.document(chat_id).get()
        if not doc.exists or (doc.to_dict() or {}).get("tenant_id") != tenant_id:
            return None
        return ChatSession(id=doc.id, **(doc.to_dict() or {}))

    def create_chat_session(self, user_id: str, tenant_id: str, title: str) -> ChatSession:
        session_data = {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "title": title,
            "created_at": datetime.now(),
        }
        doc_ref = self.chats_collection.document()
        doc_ref.set(session_data)
        return ChatSession(id=doc_ref.id, **session_data)

    def get_chat_messages(self, chat_id: str, tenant_id: str) -> List[ChatMessage]:
        session = self.get_chat_session_by_id(chat_id, tenant_id)
        if not session:
            return []
        messages_query = (
            self.chats_collection.document(chat_id)
            .collection("messages")
            .order_by("created_at", direction=firestore.Query.ASCENDING)
        )
        return [ChatMessage(**(doc.to_dict() or {})) for doc in messages_query.stream()]

    def get_chat_messages_by_date_range(self, tenant_id: str, user_id: str,
                                       start_date: datetime, end_date: datetime) -> List[ChatMessage]:
        """Belirli bir tarih aralığındaki tüm chat mesajlarını getirir."""
        from google.cloud.firestore import FieldFilter
        
        all_messages = []
        
        # Kullanıcının tüm chat session'larını al
        sessions_query = (
            self.chats_collection
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("user_id", "==", user_id))
        )
        
        for session_doc in sessions_query.stream():
            chat_id = session_doc.id
            
            # Bu session'daki mesajları tarih aralığına göre filtrele
            messages_query = (
                self.chats_collection.document(chat_id)
                .collection("messages")
                .where(filter=FieldFilter("created_at", ">=", start_date))
                .where(filter=FieldFilter("created_at", "<=", end_date))
                .order_by("created_at", direction=firestore.Query.ASCENDING)
            )
            
            for msg_doc in messages_query.stream():
                msg_data = msg_doc.to_dict()
                if msg_data:
                    all_messages.append(ChatMessage(**msg_data))
        
        return all_messages

    def save_chat_message(self, chat_id: str, tenant_id: str, message: ChatMessage):
        message_data = message.model_dump()
        self.chats_collection.document(chat_id).collection("messages").document().set(message_data)

    def delete_chat_session(self, chat_id: str, tenant_id: str) -> bool:
        try:
            doc_ref = self.chats_collection.document(chat_id)
            doc = doc_ref.get()
            if not doc.exists or (doc.to_dict() or {}).get("tenant_id") != tenant_id:
                return False

            messages_ref = doc_ref.collection("messages")
            for msg_doc in messages_ref.stream():
                msg_doc.reference.delete()

            doc_ref.delete()
            return True
        except Exception as e:
            print(f"Sohbet oturumu silinirken hata: {e}")
            return False

    # ------------------ PASSWORD RESET ------------------
    def set_password_reset_token(self, user_id: str, token: str, expires_at: datetime) -> bool:
        try:
            self.users_collection.document(user_id).update(
                {"password_reset_token": token, "token_expires_at": expires_at}
            )
            return True
        except Exception as e:
            print(f"Token ayarlanırken hata: {e}")
            return False

    def get_user_by_reset_token(self, token: str) -> Optional[UserInDB]:
        query = (
            self.users_collection.where(filter=FieldFilter("password_reset_token", "==", token))
            .limit(1)
            .stream()
        )
        for doc in query:
            user_data = doc.to_dict() or {}
            user_data["id"] = doc.id
            return UserInDB(**user_data)
        return None

    def set_user_password(self, user_id: str, hashed_password: str) -> bool:
        try:
            self.users_collection.document(user_id).update(
                {
                    "hashed_password": hashed_password,
                    "password_reset_token": firestore.DELETE_FIELD,
                    "token_expires_at": firestore.DELETE_FIELD,
                }
            )
            return True
        except Exception as e:
            print(f"Firestore'da şifre güncellenirken KRİTİK HATA (user_id: {user_id}): {e}")
            return False
