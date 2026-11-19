FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /app/requirements.txt \
    && groupadd --gid 10001 catalog && useradd --uid 10001 --gid 10001 --no-create-home catalog
COPY catalog.py /app/catalog.py
USER 10001:10001
ENTRYPOINT ["python", "/app/catalog.py"]
CMD ["verify"]
