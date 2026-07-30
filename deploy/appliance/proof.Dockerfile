FROM ghcr.io/astral-sh/uv:0.11.2 AS uv

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/services/proof/src \
    PATH=/app/services/proof/.venv/bin:$PATH

WORKDIR /app/services/proof

COPY --from=uv /uv /usr/local/bin/uv
COPY aituge_model_config/ /app/aituge_model_config/
COPY services/proof/pyproject.toml services/proof/uv.lock services/proof/README.md ./
COPY services/proof/src/ ./src/
COPY services/proof/examples/ ./examples/

RUN uv sync --frozen --no-dev --no-editable

WORKDIR /app
RUN mkdir -p /app/.proof-data

EXPOSE 18100

CMD ["sh", "-c", "python -m proof.infrastructure.postgres.migrate && exec python -m uvicorn proof.api.app:app --host 0.0.0.0 --port 18100"]
