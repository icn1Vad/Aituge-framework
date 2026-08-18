FROM ai-framework-core:base

ENV PYTHONPATH=/app:/app/backend:/app/backend/single-agent:/app/services/contract/src

WORKDIR /app

COPY backend/ ./backend/
COPY aituge_model/ ./aituge_model/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/
COPY services/contract/src/ ./services/contract/src/
COPY services/contract/capabilities/ ./services/contract/capabilities/
COPY services/contract/scripts/ ./services/contract/scripts/
COPY services/proof/capabilities/ ./services/proof/capabilities/
COPY services/travel-assistant/ ./services/travel-assistant/

RUN mkdir -p /app/localdata /app/runtime /app/backend/tool/local_runtime/artifacts

EXPOSE 8894

CMD ["python", "-m", "backend.framework_runtime"]
