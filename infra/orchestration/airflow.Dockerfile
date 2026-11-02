FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS dbt_runtime
COPY infra/dbt/requirements.txt /tmp/dbt-requirements.txt
RUN python -m venv --copies /opt/dbt/venv \
    && /opt/dbt/venv/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/dbt-requirements.txt

FROM apache/airflow:3.3.2-python3.12@sha256:9df9c8be4096b9cc626bd7cb1f2b8c712eef66c59c615f8c7e6200871ca10bd1
USER root
RUN mkdir -p /opt/airflow/state /opt/airflow/auth /opt/airflow/logs \
    && chown -R 50000:0 /opt/airflow/state /opt/airflow/auth /opt/airflow/logs
COPY --chown=50000:0 infra/orchestration/constraints.txt /opt/airflow/constraints.txt
USER airflow
COPY --chown=50000:0 infra/orchestration/duckdb-requirements.txt /opt/airflow/duckdb-requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /opt/airflow/duckdb-requirements.txt
RUN pip install --no-cache-dir 'apache-airflow==3.3.2' azure-storage-blob python-dotenv filelock \
    --constraint /opt/airflow/constraints.txt
COPY --chown=50000:0 retailflow /opt/airflow/retailflow
COPY --chown=50000:0 dags /opt/airflow/dags
COPY --chown=50000:0 infra/orchestration/start.py /opt/airflow/start.py
COPY --from=dbt_runtime /opt/dbt/venv /opt/dbt/venv
COPY infra/dbt/run.py /opt/dbt/run.py
COPY infra/dbt/project /opt/dbt/project
ENV PYTHONPATH=/opt/airflow PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/opt/airflow/start.py", "api-server"]
