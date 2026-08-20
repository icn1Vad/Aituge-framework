FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/services/qianxuesen-mentor/src \
    PATH=/app/services/qianxuesen-mentor/.venv/bin:$PATH

WORKDIR /app/services/qianxuesen-mentor
RUN pip install --no-cache-dir uv==0.11.2
COPY aituge_model/ /app/aituge_model/
COPY services/qianxuesen-mentor/pyproject.toml services/qianxuesen-mentor/uv.lock services/qianxuesen-mentor/README.md ./
COPY services/qianxuesen-mentor/src/ ./src/
RUN uv sync --frozen --no-dev --no-editable

WORKDIR /app
RUN mkdir -p /app/.qxs-data /app/corpus
EXPOSE 18400
CMD ["sh", "-c", "python -m qianxuesen_mentor.infrastructure.migrate && python -m qianxuesen_mentor.tools.import_corpus --catalog-only && exec python -m uvicorn qianxuesen_mentor.api:app --host 0.0.0.0 --port 18400"]
