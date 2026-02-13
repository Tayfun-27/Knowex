#!/bin/bash
set +e

echo "🚀 Başlatılıyor... (Mod: Multi-Database Socat Bridge)"

# ----------------------------------------------------------------
# 1. TAILSCALE HAZIRLIK VE BAŞLATMA
# ----------------------------------------------------------------
mkdir -p /tmp/tailscale
TAILSCALED_SOCK="/tmp/tailscaled.sock"
TAILSCALED_STATE="/tmp/tailscaled.state"
rm -f $TAILSCALED_SOCK

echo "⏳ Daemon (tailscaled) başlatılıyor..."
tailscaled \
    --tun=userspace-networking \
    --socket=$TAILSCALED_SOCK \
    --state=$TAILSCALED_STATE \
    --socks5-server=127.0.0.1:1055 &

# Soket dosyasının oluşmasını bekle
echo "⏳ Soket bekleniyor..."
TIMEOUT=0
while [ ! -S "$TAILSCALED_SOCK" ]; do
    sleep 1
    TIMEOUT=$((TIMEOUT+1))
    if [ $TIMEOUT -ge 15 ]; then
        echo "❌ HATA: Tailscale socket oluşmadı!"
        exit 1
    fi
done

# Tailscale ağına bağlan
echo "🔑 Tailscale'e bağlanılıyor..."
tailscale --socket=$TAILSCALED_SOCK up \
    --authkey=${TAILSCALE_AUTHKEY} \
    --hostname=cloudrun-api-${RANDOM} \
    --accept-routes \
    --ssh

# SOCKS5 Proxy portunu kontrol et
echo "⏳ Proxy portu (1055) kontrol ediliyor..."
for i in {1..30}; do
    if nc -z 127.0.0.1 1055; then
        echo "✅ Tailscale SOCKS5 aktif!"
        break
    fi
    sleep 1
done

# ----------------------------------------------------------------
# 2. VERİTABANI KÖPRÜLERİNİ (BRIDGES) KURMA
# ----------------------------------------------------------------
# Cloud Run Environment Variables üzerinden IP'leri alıyoruz.
# Eğer tanımlı değilse boş geçilir.

# --- A. MSSQL KÖPRÜSÜ (Port 1433) ---
if [ -n "$MSSQL_REMOTE_IP" ]; then
    echo "🔌 MSSQL Köprüsü kuruluyor..."
    echo "   Localhost:1433 -> Tailscale -> $MSSQL_REMOTE_IP:1433"
    socat TCP4-LISTEN:1433,fork,bind=127.0.0.1 SOCKS5:127.0.0.1:$MSSQL_REMOTE_IP:1433,socksport=1055 &
else
    echo "ℹ️  MSSQL_REMOTE_IP tanımlı değil, MSSQL köprüsü atlanıyor."
fi

# --- B. POSTGRESQL KÖPRÜSÜ (Port 5432) ---
if [ -n "$POSTGRES_REMOTE_IP" ]; then
    echo "🔌 PostgreSQL Köprüsü kuruluyor..."
    echo "   Localhost:5432 -> Tailscale -> $POSTGRES_REMOTE_IP:5432"
    socat TCP4-LISTEN:5432,fork,bind=127.0.0.1 SOCKS5:127.0.0.1:$POSTGRES_REMOTE_IP:5432,socksport=1055 &
else
    echo "ℹ️  POSTGRES_REMOTE_IP tanımlı değil, PostgreSQL köprüsü atlanıyor."
fi

# Köprülerin hazır olması için kısa bekleme
sleep 2

# ----------------------------------------------------------------
# 3. UYGULAMAYI BAŞLAT
# ----------------------------------------------------------------
echo "🚀 Gunicorn başlatılıyor..."
set -e

# Proxychains olmadan, doğrudan Gunicorn başlatıyoruz.
# Uygulama veritabanına 'localhost' üzerinden erişecek, socat tünelleyecek.
exec gunicorn main:app \
  -k uvicorn.workers.UvicornWorker \
  -w 2 \
  -b 0.0.0.0:${PORT:-8080} \
  --log-level debug \
  --capture-output \
  --timeout 120