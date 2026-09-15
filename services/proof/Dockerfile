FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN python -m venv /opt/uv \
    && /opt/uv/bin/pip install --no-cache-dir uv==0.11.2

WORKDIR /workspace/services/proof
COPY aituge_model/ /workspace/aituge_model/
COPY services/proof/pyproject.toml services/proof/uv.lock services/proof/README.md ./
RUN /opt/uv/bin/uv sync --frozen --no-dev --no-install-project

COPY services/proof/src/ ./src/
RUN /opt/uv/bin/uv sync --frozen --no-dev


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    PYTHONPATH=/app/src:/aituge_model

RUN useradd --create-home --uid 10001 aituge \
    && mkdir -p /app/.proof-data \
    && chown -R aituge:aituge /app

COPY --from=builder /workspace/aituge_model /aituge_model
COPY --from=builder --chown=aituge:aituge /workspace/services/proof /app

WORKDIR /app
USER aituge
EXPOSE 18100

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18100/health', timeout=3)" || exit 1

CMD ["sh", "-c", "python -m proof.infrastructure.postgres.migrate && exec python -m uvicorn proof.api.app:app --host 0.0.0.0 --port 18100"]
