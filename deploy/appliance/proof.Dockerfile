FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY aituge_model_config/ /aituge_model_config/
COPY services/proof/pyproject.toml services/proof/README.md ./
COPY services/proof/src/ ./src/
COPY services/proof/examples/ ./examples/

RUN pip install --no-cache-dir /aituge_model_config \
    && pip install --no-cache-dir .

RUN mkdir -p /app/.proof-data

EXPOSE 18100

CMD ["sh", "-c", "python -m proof.infrastructure.postgres.migrate && exec python -m uvicorn proof.api.app:app --host 0.0.0.0 --port 18100"]
