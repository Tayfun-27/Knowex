# backend/main.py
# Google Cloud Buildpacks için entrypoint dosyası
# Bu dosya app/main.py'daki FastAPI uygulamasını import eder

from app.main import app

# Buildpacks için: app objesini doğrudan erişilebilir yap
if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
