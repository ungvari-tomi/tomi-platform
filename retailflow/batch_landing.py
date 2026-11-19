"""Shared immutable batch publishing and verified download contract."""

import hashlib
import json
import re
from datetime import datetime
from typing import Protocol


class Store(Protocol):
    def put(self, name: str, payload: bytes, content_type: str) -> None: ...

    def read(self, name: str) -> bytes: ...


def land(
    store: Store,
    *,
    source: str,
    payload: bytes,
    extension: str,
    request: dict,
    source_metadata: dict,
    retrieved_at: datetime,
    batch_id: str,
) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", batch_id):
        raise ValueError("Batch ID must contain 1–64 letters, digits, underscores or hyphens")
    prefix = f"{source}/ingestion_date={retrieved_at.date().isoformat()}/batch={batch_id}"
    name = f"{prefix}/data.{extension}"
    digest = hashlib.sha256(payload).hexdigest()
    metadata = {
        "manifest_version": 1,
        "source": source,
        "batch_id": batch_id,
        "retrieved_at": retrieved_at.isoformat(),
        "request": request,
        "payload_blob": name,
        "format": extension,
        "encoding": "utf-8",
        "bytes": len(payload),
        "sha256": digest,
        "source_metadata": source_metadata,
    }
    content_type = "text/csv; charset=utf-8" if extension == "csv" else "application/json"
    store.put(name, payload, content_type)
    if hashlib.sha256(store.read(name)).hexdigest() != digest:
        raise ValueError("Uploaded data failed checksum verification; batch not committed")
    manifest = f"{prefix}/metadata.json"
    encoded = json.dumps(metadata, sort_keys=True, indent=2).encode("utf-8")
    store.put(manifest, encoded, "application/json")
    if store.read(manifest) != encoded:
        raise ValueError("Metadata round-trip failed")
    return manifest


def download_batch(store: Store, manifest: str) -> tuple[dict, bytes]:
    metadata = json.loads(store.read(manifest))
    payload = store.read(metadata["payload_blob"])
    if (
        len(payload) != metadata["bytes"]
        or hashlib.sha256(payload).hexdigest() != metadata["sha256"]
    ):
        raise ValueError("Downloaded batch checksum mismatch")
    return metadata, payload
