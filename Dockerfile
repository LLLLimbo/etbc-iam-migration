FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.lock pyproject.toml ./
RUN python -m pip install --no-cache-dir -r requirements.lock

COPY src ./src
COPY tests ./tests
COPY integration ./integration
COPY docker-compose.integration.yml ./docker-compose.integration.yml
COPY .dockerignore run-unit-tests.sh run-integration-tests.sh ./
RUN chmod -R a+rX ./src
RUN python -m pip install --no-cache-dir --no-deps -e .

ENTRYPOINT ["python", "-m", "etbc_migration"]
