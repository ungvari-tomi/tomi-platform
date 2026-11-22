FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp RETAILFLOW_AI_BIND=0.0.0.0
WORKDIR /app
COPY infra/analyst/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /app/requirements.txt \
    && groupadd --gid 10001 analyst \
    && useradd --uid 10001 --gid 10001 --no-create-home analyst
COPY retailflow_ai /app/retailflow_ai
COPY infra/dbt/reporting.py infra/dbt/catalog_reporting.py /app/infra/dbt/
USER 10001:10001
CMD ["python", "-m", "retailflow_ai.server"]
