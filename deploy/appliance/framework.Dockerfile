FROM python:3.11-slim AS dependencies

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PYTHONPATH=/app:/app/backend:/app/backend/single-agent

WORKDIR /app

RUN pip install --no-cache-dir poetry==2.4.1

COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root --no-interaction --no-ansi

FROM dependencies AS runtime

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY localdata/ ./localdata/
COPY scripts/ ./scripts/

RUN mkdir -p /app/runtime /app/backend/tool/local_runtime/artifacts

EXPOSE 8894

CMD ["python", "-m", "uvicorn", "backend.local_code_chat_app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8894"]
