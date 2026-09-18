FROM ai-framework-core:base

ENV PYTHONPATH=/app:/app/backend:/app/backend/single-agent:/app/services/contract/src:/app/services/business-workflow-kit

WORKDIR /app
RUN pip install --no-cache-dir mistletoe==1.5.1 python-pptx==1.0.2 xlrd==2.0.2 openpyxl==3.1.5 \
    "https://github.com/aliyun/alibabacloud-nls-python-sdk/archive/177972b1f02fcb1f2b229e77320a7286d21785f3.tar.gz"

COPY backend/ ./backend/
COPY aituge_model/ ./aituge_model/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/
COPY services/contract/src/ ./services/contract/src/
COPY services/contract/capabilities/ ./services/contract/capabilities/
COPY services/contract/scripts/ ./services/contract/scripts/
COPY services/proof/capabilities/ ./services/proof/capabilities/
COPY services/travel-assistant/ ./services/travel-assistant/
COPY services/business-workflow-kit/ ./services/business-workflow-kit/

RUN mkdir -p /app/localdata /app/runtime /app/backend/tool/local_runtime/artifacts

EXPOSE 8894

CMD ["python", "-m", "backend.framework_runtime"]
