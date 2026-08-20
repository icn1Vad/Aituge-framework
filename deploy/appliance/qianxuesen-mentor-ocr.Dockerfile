FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app
RUN pip install --no-cache-dir \
    paddlepaddle \
    paddleocr==3.7.0 \
    "paddlex[ocr]==3.7.2" \
    "psycopg[binary]>=3.2,<4" \
    "pydantic-settings>=2.4,<3" \
    "pypdf>=5,<7"
RUN apt-get update \
    && apt-get install --no-install-recommends -y libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY services/qianxuesen-mentor/src/ ./src/

CMD ["python", "-m", "qianxuesen_mentor.tools.import_corpus", "--resume", "--ocr", "--no-embed"]
