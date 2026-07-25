FROM ai-framework-core:base

ENV PYTHONPATH=/app:/app/backend:/app/backend/single-agent:/app/services/contract/src

WORKDIR /app

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/
COPY services/contract/src/ ./services/contract/src/
COPY services/contract/scripts/ ./services/contract/scripts/

RUN mkdir -p /app/localdata /app/runtime /app/backend/tool/local_runtime/artifacts

EXPOSE 8894

CMD ["python", "-m", "uvicorn", "backend.local_code_chat_app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8894"]
