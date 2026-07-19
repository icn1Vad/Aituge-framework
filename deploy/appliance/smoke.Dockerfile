FROM ai-framework-core:base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY services/smoke/src/ ./src/

EXPOSE 18200

CMD ["python", "-m", "uvicorn", "aituge_smoke.app:app", "--host", "0.0.0.0", "--port", "18200"]
