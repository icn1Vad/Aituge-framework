FROM ai-framework-core:base

WORKDIR /app

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/

RUN mkdir -p /app/localdata /app/runtime /app/backend/tool/local_runtime/artifacts

EXPOSE 8894

CMD ["python", "-m", "uvicorn", "backend.local_code_chat_app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8894"]
