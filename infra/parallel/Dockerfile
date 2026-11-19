FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY infra/parallel/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.txt \
    && groupadd --gid 10001 events && useradd --uid 10001 --gid 10001 --no-create-home events \
    && mkdir /state && chown 10001:10001 /state && chmod 2770 /state
COPY retailflow/__init__.py retailflow/api.py retailflow/currencies.py retailflow/customers.py \
    retailflow/blob_storage.py retailflow/event_contracts.py retailflow/event_ingestion.py \
    retailflow/event_runtime.py retailflow/shipment_webhook.py /app/retailflow/
USER 10001:10001
CMD ["python", "-m", "retailflow.event_runtime", "inventory-producer"]
