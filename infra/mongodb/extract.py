"""Bounded catalog reconciliation with durable frozen retries and verified landing."""

import argparse
import base64
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

from infra.mongodb.catalog import connect, documents
from retailflow.batch_landing import Store, download_batch, land
from retailflow.blob_storage import BlobStore

SOURCE = "product-catalog/products-v1"


def encode(value: object) -> bytes:
    """Canonical Extended JSON preserves BSON dates and integer widths."""
    from bson.json_util import CANONICAL_JSON_OPTIONS, dumps

    return dumps(value, json_options=CANONICAL_JSON_OPTIONS, sort_keys=True).encode("utf-8")


def save(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def reconcile(rows: list[dict], previous: dict) -> dict:
    expected = {row["_id"] for row in documents()}
    if len(rows) != 20 or {row.get("_id") for row in rows} != expected:
        raise ValueError(
            "Catalog must contain all twenty known IDs; physical deletion is unsupported"
        )
    result = {}
    for row in rows:
        if (
            set(row) != set(documents()[0])
            or row.get("synthetic") is not True
            or row.get("classification") != "synthetic_product_metadata"
            or type(row.get("schema_version")) is not int
            or row["schema_version"] != 1
            or type(row.get("revision")) is not int
            or row["revision"] < 1
        ):
            raise ValueError("Unsupported catalog contract or classification")
        identifier = row["_id"]
        entry = {"revision": row["revision"], "sha256": hashlib.sha256(encode(row)).hexdigest()}
        old = previous.get(identifier)
        if old and (
            entry["revision"] < old["revision"]
            or (entry["revision"] == old["revision"] and entry != old)
        ):
            raise ValueError("Catalog revision regressed or historical revision changed")
        result[identifier] = entry
    return result


def extract(store: Store, collection, state: Path, run_id: str) -> dict:
    """Complete the single pending batch before querying a changing source again.

    Snapshots are bounded reads, not multi-document point-in-time transactions.
    Reconciliation detects same-revision changes and hard deletion on later runs.
    """
    if not isinstance(run_id, str) or not run_id or len(run_id) > 256:
        raise ValueError("A bounded run identity is required")
    state.mkdir(parents=True, exist_ok=True)
    run_key = hashlib.sha256(run_id.encode()).hexdigest()
    with FileLock(str(state / "catalog.lock"), timeout=5, mode=0o660):
        receipts = state / "receipts"
        receipts.mkdir(exist_ok=True)
        receipt = read(receipts / f"{run_key}.json")
        if receipt is not None:
            download_batch(store, receipt["manifest"])
            return {"status": "replayed", "manifest": receipt["manifest"]}
        checkpoint_path = state / "checkpoint.json"
        pending_path = state / "pending.json"
        checkpoint = read(checkpoint_path) or {"version": 1, "sequence": 0, "products": {}}
        if checkpoint.get("version") != 1:
            raise ValueError("Unsupported catalog checkpoint")
        pending = read(pending_path)
        if pending is None:
            if checkpoint.get("run_key") == run_key:
                download_batch(store, checkpoint["manifest"])
                return {"status": "replayed", "manifest": checkpoint["manifest"]}
            rows = list(collection.find({}).sort("_id", 1).limit(21))
            products = reconcile(rows, checkpoint["products"])
            pending = {
                "version": 1,
                "sequence": checkpoint["sequence"] + 1,
                "run_key": run_key,
                "batch_id": uuid4().hex,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "payload": encode(rows).decode("utf-8"),
                "products": products,
            }
            save(pending_path, pending)
        # A crash after checkpoint replacement can leave a committed pending file.
        if pending["sequence"] == checkpoint["sequence"]:
            if pending != checkpoint.get("committed_pending"):
                raise ValueError("Pending batch conflicts with committed checkpoint")
            download_batch(store, checkpoint["manifest"])
            save(receipts / f"{pending['run_key']}.json", {"manifest": checkpoint["manifest"]})
            pending_path.unlink()
            return {"status": "recovered", "manifest": checkpoint["manifest"]}
        if pending["version"] != 1 or pending["sequence"] != checkpoint["sequence"] + 1:
            raise ValueError("Pending batch is out of sequence")
        manifest = land(
            store,
            source=SOURCE,
            payload=pending["payload"].encode("utf-8"),
            extension="json",
            request={"database": "retailflow", "collection": "products", "mode": "full_reconcile"},
            source_metadata={
                "dataset_version": "products-v1",
                "representation": "mongodb_canonical_extended_json",
                "classification": "synthetic_product_metadata",
                "row_count": 20,
                "sequence": pending["sequence"],
            },
            retrieved_at=datetime.fromisoformat(pending["retrieved_at"]),
            batch_id=pending["batch_id"],
        )
        download_batch(store, manifest)
        save(
            checkpoint_path,
            {
                "version": 1,
                "sequence": pending["sequence"],
                "run_key": pending["run_key"],
                "products": pending["products"],
                "manifest": manifest,
                "committed_pending": pending,
            },
        )
        save(receipts / f"{pending['run_key']}.json", {"manifest": manifest})
        pending_path.unlink()
        return {"status": "landed", "manifest": manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-file", type=Path, default=Path("/run/secrets/reader.json"))
    parser.add_argument("--state", type=Path, default=Path("/state"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--blob-key-file", type=Path)
    args = parser.parse_args()
    store = None
    try:
        if args.blob_key_file is not None:
            key = args.blob_key_file.read_text(encoding="utf-8").strip()
            if len(base64.b64decode(key, validate=True)) != 32:
                raise ValueError("Invalid local emulator key file")
            os.environ["AZURITE_ACCOUNT_KEY"] = key
        with connect("reader", args.credential_file) as client:
            store = BlobStore()
            store.ensure_container()
            print(json.dumps(extract(store, client.retailflow.products, args.state, args.run_id)))
    except Exception as error:
        raise SystemExit(
            f"Catalog extraction failed ({type(error).__name__}); details withheld"
        ) from None
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    main()
