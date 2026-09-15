# The image is built from pinned Python dependencies; model weights are mounted.
FROM nvidia/cuda:12.8.1-base-ubuntu24.04@sha256:133c78a0575303be34164d0b90137a042172bdf60696af01a3c424ab402d86e2
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PIP_BREAK_SYSTEM_PACKAGES=1
RUN sed -i 's|http://|https://|g' /etc/apt/sources.list.d/ubuntu.sources && apt-get update && apt-get install -y --no-install-recommends python3 python3-pip libgl1 libglib2.0-0 libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock.txt ./
RUN python3 -m pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cu128 -r requirements.lock.txt
COPY server.py mineru.json ./
ENV MINERU_TOOLS_CONFIG_JSON=/app/mineru.json MINERU_MODEL_SOURCE=local MINERU_DEVICE_MODE=cuda MINERU_API_MAX_CONCURRENT_REQUESTS=1 MINERU_PROCESSING_WINDOW_SIZE=4 OMP_NUM_THREADS=4 TOKENIZERS_PARALLELISM=false
EXPOSE 18900
CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "18900"]
