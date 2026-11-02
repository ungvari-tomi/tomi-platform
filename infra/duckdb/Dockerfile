FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RETAILFLOW_DUCKDB_PATH=/warehouse/retailflow.duckdb \
    RETAILFLOW_DUCKDB_THREADS=1 \
    RETAILFLOW_DUCKDB_MEMORY_LIMIT=256MB

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt \
    && groupadd --gid 10001 warehouse \
    && useradd --uid 10001 --gid 10001 --no-create-home warehouse \
    && mkdir /warehouse /backups \
    && chown 10001:10001 /warehouse /backups
COPY runner.py recovery.py /app/

USER 10001:10001
CMD ["python", "/app/runner.py", "serve"]
