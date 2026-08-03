FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/models

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app

RUN mkdir -p /models

COPY services/contract-ocr/pyproject.toml services/contract-ocr/README.md ./
RUN python -m pip install \
        "fastapi>=0.115,<1" \
        "uvicorn[standard]>=0.30,<1" \
        "paddleocr[doc-parser]==3.7.0" \
        "paddlepaddle==3.3.1"

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

EXPOSE 18300

CMD ["python", "-m", "uvicorn", "contract_ocr.app:app", "--host", "0.0.0.0", "--port", "18300"]
