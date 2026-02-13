# backend/app/services/mail_service.py - GÜNCELLENDİ (GENERATOR KULLANILIYOR)

import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
# Generator için typing importu eklendi
from typing import List, Dict, Any, Optional, Tuple, Generator
from datetime import datetime

def _connect_yandex_imap(email_address: str, password: str, imap_server: str, imap_port: int):
    """Yandex Mail için özel bağlantı fonksiyonu. Farklı sunucu adreslerini dener."""
    email_lower = email_address.lower()
    
    # Yandex mail için farklı sunucu adreslerini dene
    servers_to_try = []
    if imap_server:
        # .com.tr veya .ru sunucu adreslerini .com olarak normalize et
        normalized_server = imap_server.lower().strip()
        # imap.yandex.com.tr -> imap.yandex.com
        if 'imap.yandex.com.tr' in normalized_server:
            normalized_server = 'imap.yandex.com'
        # imap.yandex.ru -> imap.yandex.com
        elif 'imap.yandex.ru' in normalized_server:
            normalized_server = 'imap.yandex.com'
        # Eğer yandex.com içermiyorsa, imap.yandex.com kullan
        elif 'yandex.com' not in normalized_server:
            normalized_server = 'imap.yandex.com'
        servers_to_try.append((normalized_server, imap_port or 993))
    else:
        # Tüm Yandex mail adresleri için imap.yandex.com kullan
        servers_to_try.append(('imap.yandex.com', 993))
    
    last_error = None
    for server, port in servers_to_try:
        try:
            print(f"Yandex Mail bağlantısı deneniyor: {server}:{port}")
            mail = imaplib.IMAP4_SSL(server, port)
            mail.login(email_address, password)
            mail.select('INBOX')
            print(f"Yandex Mail bağlantısı başarılı: {server}:{port}")
            return mail, server
        except imaplib.IMAP4.error as e:
            last_error = e
            error_str = str(e)
            if 'AUTHENTICATIONFAILED' in error_str:
                # Kimlik doğrulama hatası - uygulama şifresi gerekebilir
                if 'IMAP is disabled' in error_str:
                    # IMAP devre dışı hatası - ama Outlook'ta çalışıyorsa bu yanlış olabilir
                    raise Exception(f"Yandex Mail kimlik doğrulama hatası. Lütfen uygulama şifresi kullandığınızdan emin olun. Normal şifre yerine Yandex Mail ayarlarından oluşturduğunuz uygulama şifresini kullanın. (Sunucu: {server})")
                else:
                    raise Exception(f"Yandex Mail kimlik doğrulama hatası. Lütfen uygulama şifresi kullandığınızdan emin olun. Normal şifre yerine Yandex Mail ayarlarından oluşturduğunuz uygulama şifresini kullanın. (Sunucu: {server})")
            print(f"Yandex Mail bağlantı hatası ({server}:{port}): {e}")
            continue
        except Exception as e:
            last_error = e
            print(f"Yandex Mail bağlantı hatası ({server}:{port}): {e}")
            continue
    
    # Tüm sunucular başarısız oldu
    if last_error:
        error_str = str(last_error)
        if 'AUTHENTICATIONFAILED' in error_str:
            # Tüm kimlik doğrulama hatalarında uygulama şifresi gerektiğini belirt
            raise Exception("Yandex Mail kimlik doğrulama hatası. Lütfen uygulama şifresi kullandığınızdan emin olun. Normal şifre yerine Yandex Mail ayarlarından oluşturduğunuz uygulama şifresini kullanın. Outlook'ta çalışıyorsa, muhtemelen OAuth2 kullanıyordur; bu sistem için uygulama şifresi gereklidir.")
        raise Exception(f"Yandex Mail bağlantı hatası: {last_error}")
    raise Exception("Yandex Mail bağlantısı kurulamadı. Tüm sunucu adresleri denendi.")

def test_mail_connection(email_address: str, password: str, imap_server: str, imap_port: int) -> Tuple[bool, str]:
    """Mail bağlantısını test eder."""
    try:
        email_lower = email_address.lower()
        
        # Yandex Mail için özel bağlantı yöntemi
        if 'yandex.com' in email_lower or 'yandex.com.tr' in email_lower or 'ya.ru' in email_lower:
            mail, used_server = _connect_yandex_imap(email_address, password, imap_server, imap_port)
            mail.logout()
            return True, f"Bağlantı başarılı! (Sunucu: {used_server})"
        
        # Diğer mail sağlayıcıları için normal bağlantı
        if not imap_server:
            # Varsayılan sunucular
            if 'gmail.com' in email_lower:
                imap_server = 'imap.gmail.com'
            elif 'outlook.com' in email_lower or 'hotmail.com' in email_lower:
                imap_server = 'outlook.office365.com'
            else:
                return False, "IMAP sunucu adresi belirtilmedi ve otomatik tespit edilemedi."
        
        port = imap_port or 993
        mail = imaplib.IMAP4_SSL(imap_server, port)
        mail.login(email_address, password)
        mail.select('INBOX')
        mail.logout()
        return True, "Bağlantı başarılı!"
    except imaplib.IMAP4.error as e:
        error_str = str(e)
        if 'AUTHENTICATIONFAILED' in error_str:
            if 'yandex' in email_lower:
                return False, "Yandex Mail kimlik doğrulama hatası. Lütfen uygulama şifresi kullandığınızdan emin olun. Normal şifre yerine Yandex Mail ayarlarından oluşturduğunuz uygulama şifresini kullanın."
            return False, f"Kimlik doğrulama hatası: {error_str}"
        return False, f"IMAP hatası: {error_str}"
    except Exception as e:
        return False, f"Bağlantı hatası: {str(e)}"

def decode_mime_words(s):
    """MIME encoded string'i decode eder."""
    decoded_parts = decode_header(s)
    decoded_str = ""
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            decoded_str += part.decode(encoding or 'utf-8', errors='ignore')
        else:
            decoded_str += part
    return decoded_str

def get_mail_body(msg) -> str:
    """Mail gövdesini text olarak çıkarır. HTML varsa tercih edilir."""
    html_body = ""
    text_body = ""
    
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            
            if "attachment" not in content_disposition:
                if content_type == "text/plain":
                    try:
                        payload = part.get_payload(decode=True)
                        charset = part.get_content_charset() or 'utf-8'
                        text_body += payload.decode(charset, errors='ignore')
                    except:
                        pass
                elif content_type == "text/html":
                    try:
                        payload = part.get_payload(decode=True)
                        charset = part.get_content_charset() or 'utf-8'
                        html_body += payload.decode(charset, errors='ignore')
                    except:
                        pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or 'utf-8'
            decoded = payload.decode(charset, errors='ignore')
            # Check if it's HTML
            if msg.get_content_type() == "text/html":
                html_body = decoded
            else:
                text_body = decoded
        except:
            text_body = str(msg.get_payload())
    
    # Prefer HTML over plain text
    return (html_body or text_body).strip()

# --- DEĞİŞİKLİK: DÖNÜŞ TİPİ 'Generator' OLDU ---
def fetch_mails(email_address: str, password: str, imap_server: str, imap_port: int, limit: int = 10, fetch_unread_only: bool = True, since_date: Optional[datetime] = None) -> Generator[Dict[str, Any], None, None]:
    """IMAP'ten mailleri çeker ve tek tek 'yield' eder."""
    
    # --- DEĞİŞİKLİK: mails = [] listesi kaldırıldı ---
    mail_conn = None
    
    try:
        email_lower = email_address.lower()
        
        # Yandex Mail için özel bağlantı yöntemi
        if 'yandex.com' in email_lower or 'yandex.com.tr' in email_lower or 'ya.ru' in email_lower:
            print(f"Yandex Mail için özel bağlantı yöntemi kullanılıyor...")
            mail_conn, used_server = _connect_yandex_imap(email_address, password, imap_server, imap_port)
            print(f"Yandex Mail bağlantısı başarılı: {used_server}")
        else:
            # Diğer mail sağlayıcıları için normal bağlantı
            if not imap_server:
                if 'gmail.com' in email_lower:
                    imap_server = 'imap.gmail.com'
                elif 'outlook.com' in email_lower or 'hotmail.com' in email_lower:
                    imap_server = 'outlook.office365.com'
                else:
                    raise Exception("IMAP sunucu adresi belirtilmedi ve otomatik tespit edilemedi. Lütfen IMAP sunucu adresini manuel olarak girin.")
            
            port = imap_port or 993
            print(f"IMAP bağlantısı kuruluyor: {imap_server}:{port}")
            mail_conn = imaplib.IMAP4_SSL(imap_server, port)
            
            print(f"Mail hesabına giriş yapılıyor: {email_address}")
            mail_conn.login(email_address, password)
        
        print("INBOX seçiliyor...")
        status, _ = mail_conn.select('INBOX')
        if status != 'OK':
            mail_conn.logout()
            raise Exception("INBOX seçilemedi. Mail sunucusu yanıt vermiyor.")
        
        # Mail listesi al - fetch_unread_only ve since_date ayarlarına göre
        search_criteria = []
        
        if since_date:
            # IMAP tarih formatı: DD-MMM-YYYY (örn: 01-Jan-2024)
            date_str = since_date.strftime('%d-%b-%Y')
            search_criteria.append(f'SINCE {date_str}')
            print(f"📅 Tarih filtresi: {date_str} tarihinden itibaren mailler aranıyor...")
        
        if fetch_unread_only:
            search_criteria.append('UNSEEN')
            print("📭 Okunmamış mail listesi alınıyor...")
        else:
            print("📧 Tüm mail listesi alınıyor...")
        
        # Arama kriterlerini birleştir
        search_query = ' '.join(search_criteria) if search_criteria else 'ALL'
        print(f"🔍 IMAP Search Sorgusu: '{search_query}' (fetch_unread_only={fetch_unread_only}, since_date={since_date})")
        status, messages = mail_conn.search(None, search_query)
        
        if status != 'OK':
            mail_conn.logout()
            raise Exception(f"Mail listesi alınamadı. Sunucu yanıtı: {status}")
        
        email_ids = messages[0].split()
        if not email_ids:
            mail_conn.logout()
            print(f"⚠️ IMAP search sonucu: Hiç mail bulunamadı (sorgu: '{search_query}')")
            # --- DEĞİŞİKLİK: Boş liste döndürmek yerine generator'ı durdur ---
            return
        
        # Tüm mailleri işle (limit uygulanmıyor)
        print(f"✅ IMAP search sonucu: {len(email_ids)} mail bulundu (sorgu: '{search_query}', fetch_unread_only={fetch_unread_only})")
        print(f"📬 {len(email_ids)} mail bulundu, işleniyor...")
        
        for email_id in reversed(email_ids):
            try:
                # BODY.PEEK[] kullan - maili okunmuş yapmaz
                status, msg_data = mail_conn.fetch(email_id, '(BODY.PEEK[])')
                if status != 'OK' or not msg_data or not msg_data[0]:
                    print(f"Mail fetch hatası (ID: {email_id}): {status}")
                    continue
                
                raw_email = msg_data[0][1]
                if not raw_email:
                    continue
                    
                msg = email.message_from_bytes(raw_email)
                
                # Header bilgileri
                subject = decode_mime_words(msg.get("Subject", ""))
                sender = decode_mime_words(msg.get("From", ""))
                date_str = msg.get("Date", "")
                received_at = datetime.now()
                if date_str:
                    try:
                        received_at = parsedate_to_datetime(date_str)
                    except:
                        pass
                
                # Message-ID'yi al (duplicate kontrolü için)
                message_id = msg.get("Message-ID", "").strip()
                if not message_id:
                    # Message-ID yoksa IMAP UID'yi kullan
                    message_id = f"imap_uid_{email_id.decode() if isinstance(email_id, bytes) else str(email_id)}"
                
                # Mailleşme zinciri header'ları (thread gruplama için)
                in_reply_to = msg.get("In-Reply-To", "").strip()  # Bu mail hangi mailin cevabı?
                references = msg.get("References", "").strip()     # Mailleşme zincirindeki tüm Message-ID'ler
                
                # Body
                body = get_mail_body(msg)
                if not body:
                    body = "(İçerik bulunamadı)"
                
                # Attachments
                attachments = []
                if msg.is_multipart():
                    for part in msg.walk():
                        content_disposition = str(part.get("Content-Disposition", ""))
                        if "attachment" in content_disposition:
                            filename = part.get_filename()
                            if filename:
                                filename = decode_mime_words(filename)
                                try:
                                    # --- DEĞİŞİKLİK: Payload'ı burada yüklüyoruz (bu beklenen bir durum) ---
                                    # Bellek sorunu, bu payload'ın bir listede biriktirilmesinden kaynaklanıyordu.
                                    # yield kullandığımız için bu payload bir sonraki döngüde serbest kalacak.
                                    payload = part.get_payload(decode=True)
                                    size = len(payload) if payload else 0
                                except:
                                    payload = None
                                    size = 0
                                attachments.append({
                                    "filename": filename,
                                    "content_type": part.get_content_type(),
                                    "size": size,
                                    "payload": payload  # Ek içeriği için
                                })
                
                # --- DEĞİŞİKLİK: 'mails.append' yerine 'yield' kullanıldı ---
                yield {
                    "sender": sender,
                    "subject": subject,
                    "body": body,
                    "received_at": received_at,
                    "attachments": attachments,
                    "email_id": email_id.decode() if isinstance(email_id, bytes) else str(email_id),
                    "message_id": message_id,
                    "in_reply_to": in_reply_to,  # Mailleşme zinciri için
                    "references": references      # Mailleşme zinciri için
                }
                
                # Yield sonrası temizlik - bellek optimizasyonu
                del msg
                del raw_email
                # Attachments zaten yield edildi, temizleme mail.py'de yapılacak
                import gc
                gc.collect()
                
            except Exception as e:
                print(f"Mail parse hatası (ID: {email_id}): {e}")
                import traceback
                print(traceback.format_exc())
                # Hata durumunda da temizlik yap
                if 'msg' in locals():
                    del msg
                if 'raw_email' in locals():
                    del raw_email
                import gc
                gc.collect()
                continue
        
        if mail_conn:
            mail_conn.logout()
        # --- DEĞİŞİKLİK: 'return mails' kaldırıldı ---
        print("Mail çekme işlemi tamamlandı.")
        
    except imaplib.IMAP4.error as e:
        if mail_conn:
            try: mail_conn.logout()
            except: pass
        error_msg = f"IMAP hatası: {str(e)}"
        print(f"IMAP hatası: {e}")
        raise Exception(error_msg)
    except Exception as e:
        if mail_conn:
            try: mail_conn.logout()
            except: pass
        error_msg = f"Mail alma hatası: {str(e)}"
        print(f"Mail alma hatası: {e}")
        import traceback
        print(traceback.format_exc())
        raise Exception(error_msg)

def fetch_single_mail_body(email_address: str, password: str, imap_server: str, imap_port: int, message_id: str) -> Optional[str]:
    """
    Belirli bir mailin body içeriğini IMAP'ten çeker (on-demand).
    Mail DB'de body=None olarak kaydedilmişse, bu fonksiyon kullanılır.
    """
    mail_conn = None
    try:
        email_lower = email_address.lower()
        
        # Yandex Mail için özel bağlantı yöntemi
        if 'yandex.com' in email_lower or 'yandex.com.tr' in email_lower or 'ya.ru' in email_lower:
            mail_conn, used_server = _connect_yandex_imap(email_address, password, imap_server, imap_port)
            # _connect_yandex_imap zaten INBOX'u seçiyor
        else:
            # Diğer mail sağlayıcıları için normal bağlantı
            if not imap_server:
                if 'gmail.com' in email_lower:
                    imap_server = 'imap.gmail.com'
                elif 'outlook.com' in email_lower or 'hotmail.com' in email_lower:
                    imap_server = 'outlook.office365.com'
                else:
                    raise Exception("IMAP sunucu adresi belirtilmedi ve otomatik tespit edilemedi.")
            
            port = imap_port or 993
            mail_conn = imaplib.IMAP4_SSL(imap_server, port)
            mail_conn.login(email_address, password)
            mail_conn.select('INBOX')
        
        # Message-ID ile maili bul
        status, messages = mail_conn.search(None, f'HEADER Message-ID "{message_id}"')
        
        if status != 'OK' or not messages[0]:
            mail_conn.logout()
            return None
        
        email_ids = messages[0].split()
        if not email_ids:
            mail_conn.logout()
            return None
        
        # İlk eşleşen maili al
        email_id = email_ids[0]
        status, msg_data = mail_conn.fetch(email_id, '(BODY.PEEK[])')
        
        if status != 'OK' or not msg_data or not msg_data[0]:
            mail_conn.logout()
            return None
        
        raw_email = msg_data[0][1]
        if not raw_email:
            mail_conn.logout()
            return None
        
        msg = email.message_from_bytes(raw_email)
        body = get_mail_body(msg)
        
        mail_conn.logout()
        return body if body else None
        
    except Exception as e:
        if mail_conn:
            try:
                mail_conn.logout()
            except:
                pass
        print(f"Mail body çekme hatası: {e}")
        return None