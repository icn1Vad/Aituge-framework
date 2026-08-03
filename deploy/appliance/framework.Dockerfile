FROM python:3.11-slim AS dependencies

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PYTHONPATH=/app:/app/backend:/app/backend/single-agent:/app/services/contract/src

WORKDIR /app

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir poetry==2.4.1

COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root --no-interaction --no-ansi

FROM dependencies AS runtime

COPY backend/ ./backend/
COPY aituge_model/ ./aituge_model/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/
COPY services/contract/src/ ./services/contract/src/
COPY services/contract/capabilities/ ./services/contract/capabilities/
COPY services/contract/scripts/ ./services/contract/scripts/

RUN mkdir -p /app/localdata /app/runtime /app/backend/tool/local_runtime/artifacts

EXPOSE 8894

CMD ["python", "-m", "backend.framework_runtime"]
