FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_INSTALLER_PARALLEL=false \
    POETRY_SYSTEM_GIT_CLIENT=true \
    POETRY_VIRTUALENVS_CREATE=false \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

ADD --checksum=sha256:aaea98d7afbb0a6e2287c0c2b40ed91325f60efc965aba678d55c44031c09441 \
    https://codeload.github.com/aliyun/alibabacloud-nls-python-sdk/tar.gz/177972b1f02fcb1f2b229e77320a7286d21785f3 \
    /tmp/nls-sdk.tar.gz

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv "$VIRTUAL_ENV" \
    && pip install --no-cache-dir poetry==2.2.1

WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root \
    && pip install --no-cache-dir --no-deps /tmp/nls-sdk.tar.gz

COPY . .
RUN poetry install --only main


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app:/app/backend:/app/backend/single-agent:/app/services/contract/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends git libgomp1 poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 aituge \
    && mkdir -p /app/runtime/attachments /app/runtime/chat-artifacts /app/runtime/code-runs /app/data/contract \
    && chown -R aituge:aituge /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=aituge:aituge /app /app

WORKDIR /app
USER aituge
EXPOSE 8894 18300

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8894/openapi.json', timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "backend.local_code_chat_app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8894"]
