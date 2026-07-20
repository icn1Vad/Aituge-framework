FROM python:3.11-slim AS dependencies

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PYTHONPATH=/app:/app/backend:/app/backend/single-agent

WORKDIR /app

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        fontconfig \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir poetry==2.4.1

COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root --no-interaction --no-ansi

COPY services/iron_report/pyproject.toml ./services/iron_report/pyproject.toml
COPY services/iron_report/src/ ./services/iron_report/src/
RUN pip install --no-cache-dir ./services/iron_report

FROM dependencies AS runtime-base

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY services/iron_report/ ./services/iron_report/
COPY tests/ ./tests/
COPY conftest.py pytest.ini ./

RUN mkdir -p /app/runtime /app/aituge-tmp/code-runs /app/aituge-tmp/chat-artifacts \
    && chmod -R a-w /app/services/iron_report/demo_data

EXPOSE 18300

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=30 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18300/health', timeout=3)"

FROM runtime-base AS test
RUN pytest -q services/iron_report/tests

FROM runtime-base AS runtime
CMD ["python", "-m", "uvicorn", "iron_report.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "18300"]
