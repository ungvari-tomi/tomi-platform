FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY infra/orchestration/api-requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.txt \
    && groupadd --gid 10001 api && useradd --uid 10001 --gid 10001 --no-create-home api
COPY retailflow/__init__.py retailflow/api.py retailflow/currencies.py /app/retailflow/
USER 10001:10001
CMD ["python", "-m", "uvicorn", "retailflow.api:app", "--host", "0.0.0.0", "--port", "8000"]
