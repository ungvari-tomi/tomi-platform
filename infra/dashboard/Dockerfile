FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp \
    RETAILFLOW_REPORTING_DIR=/reports \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_HEADLESS=true STREAMLIT_GLOBAL_DEVELOPMENT_MODE=false
WORKDIR /app
COPY infra/dashboard/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /app/requirements.txt \
    && groupadd --gid 10001 dashboard \
    && useradd --uid 10001 --gid 10001 --no-create-home dashboard \
    && mkdir /reports && chown 10001:10001 /reports && chmod 2770 /reports
COPY infra/dashboard/app.py /app/app.py
COPY infra/dbt/reporting.py /app/reporting.py
COPY infra/dbt/catalog_reporting.py /app/catalog_reporting.py
COPY infra/dashboard/catalog_page.py /app/catalog_page.py
USER 10001:10001
EXPOSE 8501
CMD ["streamlit", "run", "/app/app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.enableCORS=true", "--server.enableXsrfProtection=true", "--client.toolbarMode=viewer"]
