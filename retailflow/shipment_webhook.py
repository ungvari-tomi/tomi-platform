"""Authenticated local webhook: acknowledge only verified landing and warehouse commit."""

import hashlib
import hmac
import os
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from retailflow.blob_storage import BlobStore
from retailflow.event_contracts import ShipmentUpdate, decode
from retailflow.event_ingestion import ingest

KEY_FILE = Path(".local-secrets/shipment-webhook.key")


def initialize_key() -> None:
    KEY_FILE.parent.mkdir(mode=0o700, exist_ok=True)
    if KEY_FILE.exists():
        read_key(KEY_FILE)
        return
    descriptor = os.open(KEY_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(secrets.token_bytes(32))


def read_key(path: Path) -> bytes:
    try:
        value = path.read_bytes()
        if len(value) != 32:
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise ValueError("Shipment webhook key file is missing or invalid") from None


def signature(key: bytes, timestamp: str, body: bytes) -> str:
    return hmac.new(key, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def create_app(
    *,
    store_factory: Callable = BlobStore,
    database: Path | None = None,
    key: bytes | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    service = FastAPI(title="RetailFlow shipment webhook", docs_url=None, redoc_url=None)
    if key is not None and len(key) != 32:
        raise ValueError("Webhook authentication requires a 32-byte key")

    @service.get("/health")
    def health():
        return {"status": "ok", "service": "shipment-webhook"}

    @service.post("/v1/shipments/events", status_code=202)
    async def receive(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise HTTPException(415, "JSON event required")
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > 16384:
                raise HTTPException(413, "Event exceeds the payload limit")
            payload.extend(chunk)
        body = bytes(payload)
        timestamp = request.headers.get("x-retailflow-timestamp", "")
        provided = request.headers.get("x-retailflow-signature", "")
        if not re.fullmatch(r"[0-9]{10,11}", timestamp) or abs(clock() - int(timestamp)) > 300:
            raise HTTPException(401, "Invalid event authentication")
        try:
            secret = (
                key
                if key is not None
                else read_key(Path(os.environ.get("RETAILFLOW_SHIPMENT_KEY_FILE", str(KEY_FILE))))
            )
        except ValueError:
            raise HTTPException(503, "Webhook authentication is unavailable") from None
        if not re.fullmatch(r"[a-f0-9]{64}", provided) or not hmac.compare_digest(
            provided, signature(secret, timestamp, body)
        ):
            raise HTTPException(401, "Invalid event authentication")
        try:
            event = decode(body)
            if not isinstance(event, ShipmentUpdate):
                raise ValueError()
        except ValueError:
            raise HTTPException(422, "Event violates the shipment contract") from None

        def persist():
            store = store_factory()
            try:
                if hasattr(store, "ensure_container"):
                    store.ensure_container()
                return ingest(store, event, database or Path(os.environ["RETAILFLOW_DUCKDB_PATH"]))
            finally:
                if hasattr(store, "close"):
                    store.close()

        try:
            result = await run_in_threadpool(persist)
        except Exception:
            # Never echo signed bodies, secrets or rejected source values into HTTP/logs.
            raise HTTPException(503, "Event could not be committed; retry the same event") from None
        return {"status": "committed", "already_loaded": result["already_loaded"]}

    return service


app = create_app()

if __name__ == "__main__":
    initialize_key()
    print("Local shipment webhook key file ready; key not displayed.")
