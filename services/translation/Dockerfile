FROM ai-framework-core:base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/backend:/app/backend/single-agent:/app/services/translation/src \
    TRANSLATION_DATA_ROOT=/data

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

COPY backend/ ./backend/
COPY services/translation/ ./services/translation/

RUN pip install --no-cache-dir ./services/translation \
    && groupadd --system translation \
    && useradd --system --gid translation --home-dir /app translation \
    && mkdir -p /data \
    && chown -R translation:translation /app /data

USER translation

HEALTHCHECK --interval=20s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18500/healthz', timeout=3).read()"

CMD ["python", "-m", "uvicorn", "translation_service.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "18500"]
