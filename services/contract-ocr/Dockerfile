FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/models \
    PADDLE_HOME=/models/paddle \
    PADDLE_PDX_CACHE_HOME=/models/paddlex

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app

RUN mkdir -p /models

COPY services/contract-ocr/pyproject.toml services/contract-ocr/README.md ./
RUN python -m pip install \
        "fastapi>=0.115,<1" \
        "uvicorn[standard]>=0.30,<1" \
        "paddleocr[doc-parser]==3.7.0" \
        "paddlepaddle==3.2.2" \
        "pypdf>=5,<7" \
        "python-docx>=1.1,<2" \
        "python-multipart>=0.0.9,<1"

COPY services/contract-ocr/src/ ./src/
RUN python -m pip install --no-deps .

# PaddlePaddle and OpenCV need OpenMP, GL, and GLib runtimes. The standard slim
# image does not include them, so install only those runtime packages from the
# appliance's domestic Debian mirror. Kept after Python dependencies to preserve
# Docker cache for the much larger Paddle installation.
RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|; s|http://deb.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|' /etc/apt/sources.list.d/debian.sources \
    && DEBIAN_FRONTEND=noninteractive apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends libgomp1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# PPStructureV3 normally downloads weights at the first document request.
# Warm them in the image layer so offline appliance startup is deterministic.
RUN python - <<'PY'
from paddleocr import PPStructureV3

PPStructureV3(lang="ch", use_table_recognition=True)
PY

EXPOSE 18300

CMD ["python", "-m", "uvicorn", "contract_ocr.app:app", "--host", "0.0.0.0", "--port", "18300"]
