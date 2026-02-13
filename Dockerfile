# backend/Dockerfile

# Python 3.12 imajını temel al
FROM python:3.12-slim

# Performans ve Loglama ayarları
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

# Çalışma dizini
WORKDIR /app

# Sistem bağımlılıklarını kur
# dos2unix: Windows formatındaki scriptleri düzeltmek için
# netcat-openbsd: start.sh içinde port kontrolü için (nc komutu)
# findutils: start.sh içinde dosya arama için (find komutu)
RUN apt-get update && apt-get install -y \
    curl \
    gnupg2 \
    unixodbc-dev \
    g++ \
    libmagic1 \
    ca-certificates \
    dos2unix \
    # 1. Microsoft GPG ve Repo
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | \
    gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list \
    # 2. Tailscale Reposu ve Anahtarı
    && curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg | \
    tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null \
    && echo "deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/debian bookworm main" | \
    tee /etc/apt/sources.list.d/tailscale.list \
    # 3. Paketleri Yükle (netcat ve findutils EKLENDİ)
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y msodbcsql18 tailscale socat netcat-openbsd findutils \
    && rm -rf /var/lib/apt/lists/*

# Bağımlılıkları kopyala ve kur
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt

# Uygulama kodlarını kopyala
COPY ./app /app/app
COPY main.py /app/main.py

# Başlatma dosyalarını kopyala
COPY start.sh /app/start.sh
COPY proxychains.conf /etc/proxychains.conf

# Dosya formatını ve izinleri düzelt
RUN dos2unix /app/start.sh && chmod +x /app/start.sh

EXPOSE 8080

# Güvenlik: Root olmayan kullanıcı
RUN addgroup --system --gid 1001 python && \
    adduser --system --uid 1001 --ingroup python --home /home/python python && \
    chown -R python:python /app

USER python

# Başlat
CMD ["/app/start.sh"]