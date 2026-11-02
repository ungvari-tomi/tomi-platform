"""Land orders as CSV and unmodified Frankfurter response bodies as JSON."""

import argparse
import csv
import hashlib
import io
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import httpx

from retailflow.api import Order, OrderPage
from retailflow.blob_storage import BlobStore
from retailflow.currencies import FX_QUOTES, SUPPORTED_DATASETS
from retailflow.exchange_rates import fetch_eur_rate_response


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


def land_orders(
    store: Store, response: httpx.Response, *, batch_id: str, retrieved_at: datetime
) -> str:
    response.raise_for_status()
    body = response.json()
    page = OrderPage.model_validate(body)
    if body.get("dataset_version") not in SUPPORTED_DATASETS:
        raise ValueError("Unsupported orders dataset version")
    if any(set(row) != set(Order.model_fields) for row in body["orders"]):
        raise ValueError("Order schema changed; refusing a lossy CSV conversion")
    if any(row.total_minor != row.quantity * row.unit_price_minor for row in page.orders):
        raise ValueError("Invalid order total")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(Order.model_fields), lineterminator="\n")
    writer.writeheader()
    writer.writerows(row.model_dump(mode="json") for row in page.orders)
    return land(
        store,
        source="orders",
        payload=buffer.getvalue().encode("utf-8"),
        extension="csv",
        request={"url": str(response.request.url)},
        source_metadata={
            **page.model_dump(mode="json", exclude={"orders"}),
            "row_count": len(page.orders),
            "columns": list(Order.model_fields),
            "representation": "converted_from_json",
            "source_response_sha256": hashlib.sha256(response.content).hexdigest(),
        },
        retrieved_at=retrieved_at,
        batch_id=batch_id,
    )


def download_batch(store: Store, manifest: str) -> tuple[dict, bytes]:
    metadata = json.loads(store.read(manifest))
    payload = store.read(metadata["payload_blob"])
    if (
        len(payload) != metadata["bytes"]
        or hashlib.sha256(payload).hexdigest() != metadata["sha256"]
    ):
        raise ValueError("Downloaded batch checksum mismatch")
    return metadata, payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="source", required=True)
    orders = sub.add_parser("orders")
    orders.add_argument("--after-id", type=int)
    orders.add_argument("--limit", type=int, default=20)
    rates = sub.add_parser("exchange-rates")
    rates.add_argument("--date", type=date.fromisoformat, required=True)
    rates.add_argument("--quote", choices=FX_QUOTES, default="HUF")
    for command in (orders, rates):
        command.add_argument("--batch-id", default=None)
    download = sub.add_parser("download")
    download.add_argument("--manifest", required=True)
    download.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    store = BlobStore()
    try:
        store.ensure_container()
        if args.source == "download":
            metadata, payload = download_batch(store, args.manifest)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            # Avoid silently replacing an existing local file.
            with args.output.open("xb") as handle:
                handle.write(payload)
            print(
                json.dumps(
                    {"status": "verified", "format": metadata["format"], "bytes": len(payload)}
                )
            )
            return
        batch_id = args.batch_id or uuid4().hex
        if args.source == "orders":
            params = {"limit": args.limit}
            if args.after_id is not None:
                params["after_id"] = args.after_id
            with httpx.Client(timeout=10, trust_env=False) as client:
                response = client.get("http://127.0.0.1:8000/v1/orders", params=params)
            manifest = land_orders(
                store, response, batch_id=batch_id, retrieved_at=datetime.now(UTC)
            )
        else:
            observation, response = fetch_eur_rate_response(args.date, quote=args.quote)
            manifest = land(
                store,
                source="exchange-rates",
                payload=response.content,
                extension="json",
                request={"url": str(response.request.url)},
                source_metadata=observation.model_dump(mode="json"),
                retrieved_at=datetime.now(UTC),
                batch_id=batch_id,
            )
        print(json.dumps({"status": "landed", "manifest": manifest}))
    finally:
        store.close()


if __name__ == "__main__":
    main()
