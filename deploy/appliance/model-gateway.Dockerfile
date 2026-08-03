FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    MODEL_CONFIG_DIR=/app/aituge_model/config

WORKDIR /app

COPY aituge_model/ /app/aituge_model/

RUN pip install --no-cache-dir '/app/aituge_model[gateway]'

EXPOSE 18300

CMD ["python", "-m", "aituge_model.gateway"]
