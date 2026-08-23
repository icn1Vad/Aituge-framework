FROM python:3.12-slim AS dependencies

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    MODEL_CONFIG_DIR=/aituge_model/config

WORKDIR /app

COPY aituge_model/ /aituge_model/
COPY services/contract/pyproject.toml services/contract/README.md ./

RUN pip install --no-cache-dir /aituge_model \
    && python -c "import subprocess, sys, tomllib; data = tomllib.load(open('pyproject.toml', 'rb')); subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', 'hatchling>=1.27', *data['project']['dependencies']])"

FROM dependencies AS test

RUN python -c "import subprocess, sys, tomllib; data = tomllib.load(open('pyproject.toml', 'rb')); subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', *data['project']['optional-dependencies']['test']])"
COPY services/contract/src/ ./src/
COPY services/contract/capabilities/revision_drafts.py ./services/contract/capabilities/revision_drafts.py
COPY services/contract/capabilities/model_observation.py ./services/contract/capabilities/model_observation.py
COPY services/contract/capabilities/grounded_answer.py ./services/contract/capabilities/grounded_answer.py
COPY services/contract/tests/ ./tests/
COPY services/contract/openapi/ ./openapi/
COPY services/contract/scripts/ ./scripts/
RUN pip install --no-cache-dir --no-deps --no-build-isolation .

CMD ["python", "-m", "pytest", "-q"]

FROM dependencies AS runtime

ENV CONTRACT_MOCK_MODE=false

COPY services/contract/src/ ./src/
COPY services/contract/capabilities/revision_drafts.py ./services/contract/capabilities/revision_drafts.py
COPY services/contract/capabilities/model_observation.py ./services/contract/capabilities/model_observation.py
RUN pip install --no-cache-dir --no-deps --no-build-isolation .

RUN mkdir -p /app/data/contract

EXPOSE 18200

CMD ["sh", "-c", "python -m contract.persistence.postgres.migrate && exec python -m uvicorn contract.api.app:app --host 0.0.0.0 --port 18200"]
