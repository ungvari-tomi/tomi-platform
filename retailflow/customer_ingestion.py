"""Versioned customer demo: minimize before staging/landing; load only masked data."""

import argparse
import csv
import hashlib
import io
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import duckdb
import httpx
from filelock import FileLock
from pydantic import ValidationError

from retailflow.api import CustomerOrderPage
from retailflow.blob_storage import BlobStore
from retailflow.currencies import valid_price
from retailflow.customers import CustomerPage
from retailflow.extraction import load, save
from retailflow.landing import Store
from retailflow.privacy import (
    PrivacyKey,
    PrivateCustomer,
    PrivateOrder,
    initialize_key,
    minimize,
    prepare_key_file,
)


def validate_safe(stage: dict) -> tuple[list[PrivateOrder], list[PrivateCustomer]]:
    try:
        expected = {
            "contract",
            "key_id",
            "seed",
            "interval_seconds",
            "as_of",
            "orders",
            "customers",
        }
        if set(stage) != expected or stage["contract"] != "customer-pseudonym-v1":
            raise ValueError()
        # Metadata is allowlisted too: no URL, raw response hash or free-text source metadata.
        if not isinstance(stage["key_id"], str) or not re.fullmatch(
            r"[a-z0-9-]{1,24}", stage["key_id"]
        ):
            raise ValueError()
        if type(stage["seed"]) is not int or type(stage["interval_seconds"]) is not int:
            raise ValueError()
        if not 1 <= stage["interval_seconds"] <= 3600:
            raise ValueError()
        if date.fromisoformat(stage["as_of"]).isoformat() != stage["as_of"]:
            raise ValueError()
        orders = [PrivateOrder.model_validate(row) for row in stage["orders"]]
        customers = [PrivateCustomer.model_validate(row) for row in stage["customers"]]
        referenced = {row.customer_token for row in orders}
        lookup = {row.customer_token: row for row in customers}
        if len(orders) != len({row.order_id for row in orders}) or len(customers) != len(lookup):
            raise ValueError()
        if set(lookup) != referenced or not orders or len(orders) > 100:
            raise ValueError()
        prefix = "ct1_" + stage["key_id"] + "_"
        if any(not token.startswith(prefix) for token in lookup):
            raise ValueError()
        if any(row.country != lookup[row.customer_token].country for row in orders):
            raise ValueError()
        if any(row.total_minor != row.quantity * row.unit_price_minor for row in orders):
            raise ValueError()
        if any(
            not valid_price(row.country, row.currency, row.unit_price_minor, "orders-v3")
            or row.created_at.utcoffset() is None
            for row in orders
        ):
            raise ValueError()
        return orders, customers
    except (KeyError, TypeError, ValueError, ValidationError):
        raise ValueError("Masked customer batch violates the privacy contract") from None


def retrieve(client: httpx.Client, key: PrivacyKey, *, after_id: int | None, limit: int) -> dict:
    try:
        params = {"limit": limit}
        if after_id is not None:
            params["after_id"] = after_id
        response = client.get("/v2/orders", params=params)
        response.raise_for_status()
        orders = CustomerOrderPage.model_validate(response.json())
        if not orders.orders or orders.observed_at.utcoffset() is None:
            raise ValueError("No valid referenced orders")
        if any(
            row.created_at.utcoffset() is None or row.created_at > orders.observed_at
            for row in orders.orders
        ):
            raise ValueError("Invalid order clock")
        response = client.get(
            "/v2/customers", params=[("order_ids", row.order_id) for row in orders.orders]
        )
        response.raise_for_status()
        customers = CustomerPage.model_validate(response.json())
        safe_orders, safe_customers = minimize(orders, customers, key)
        stage = {
            "contract": "customer-pseudonym-v1",
            "key_id": key.key_id,
            "seed": orders.seed,
            "interval_seconds": orders.interval_seconds,
            "as_of": orders.observed_at.astimezone(UTC).date().isoformat(),
            "orders": safe_orders,
            "customers": safe_customers,
        }
        validate_safe(stage)
        return stage
    except (ValidationError, ValueError, KeyError, TypeError):
        # Pydantic errors contain rejected input values. Never propagate those errors.
        raise ValueError("Synthetic customer response failed privacy validation") from None


def land_private(store: Store, stage: dict, batch_id: str) -> str:
    orders, customers = validate_safe(stage)
    if len(batch_id) != 32 or any(character not in "0123456789abcdef" for character in batch_id):
        raise ValueError("Invalid private batch identity")
    prefix = f"customer-orders/as_of={stage['as_of']}/batch={batch_id}"
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(PrivateOrder.model_fields), lineterminator="\n")
    writer.writeheader()
    writer.writerows(row.model_dump(mode="json") for row in orders)
    payloads = {
        "orders.csv": buffer.getvalue().encode(),
        "customers.json": json.dumps(
            [row.model_dump(mode="json") for row in customers], sort_keys=True
        ).encode(),
    }
    metadata = {key: value for key, value in stage.items() if key not in {"orders", "customers"}}
    metadata.update(manifest_version=1, files={})
    for name, payload in payloads.items():
        path = prefix + "/" + name
        digest = hashlib.sha256(payload).hexdigest()
        store.put(path, payload, "text/csv" if name.endswith("csv") else "application/json")
        if store.read(path) != payload:
            raise ValueError("Masked upload checksum verification failed")
        metadata["files"][name] = {"sha256": digest, "bytes": len(payload)}
    manifest = prefix + "/metadata.json"
    encoded = json.dumps(metadata, sort_keys=True, indent=2).encode()
    store.put(manifest, encoded, "application/json")  # Commit marker is published last.
    if store.read(manifest) != encoded:
        raise ValueError("Masked manifest verification failed")
    return manifest


def extract_private(
    store: Store,
    client: httpx.Client,
    state: Path,
    run_id: str,
    key: PrivacyKey,
    *,
    after_id: int | None = None,
    limit: int = 20,
    expected_profile: tuple[int, int] | None = None,
) -> str:
    if not 1 <= limit <= 100:
        raise ValueError("Private batches require 1-100 orders")
    identity = hashlib.sha256(run_id.encode()).hexdigest()[:32]
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state / (identity + ".json")
    with FileLock(str(path) + ".lock", timeout=5):
        stage = load(path)
        if stage is None:
            stage = retrieve(client, key, after_id=after_id, limit=limit)
            save(path, stage)  # Only minimized data is ever staged, including on failure/retry.
        elif stage.get("key_id") != key.key_id:
            raise ValueError("Privacy key ID changed during batch retry")
        if expected_profile and (stage["seed"], stage["interval_seconds"]) != expected_profile:
            raise ValueError("Customer source profile differs from scheduled configuration")
        return land_private(store, stage, identity)


SCHEMA = """
CREATE SCHEMA IF NOT EXISTS privacy_demo;
CREATE TABLE IF NOT EXISTS privacy_demo.orders (
 dataset_id VARCHAR, order_id BIGINT, created_at TIMESTAMPTZ, country VARCHAR,
 currency VARCHAR, product_id VARCHAR, quantity INTEGER, unit_price_minor BIGINT,
 total_minor BIGINT, customer_token VARCHAR, PRIMARY KEY(dataset_id,order_id));
CREATE TABLE IF NOT EXISTS privacy_demo.customers (
 dataset_id VARCHAR, customer_token VARCHAR, as_of DATE, country VARCHAR,
 age_band VARCHAR, synthetic BOOLEAN, PRIMARY KEY(dataset_id,customer_token,as_of));
CREATE TABLE IF NOT EXISTS privacy_demo.batches (
 manifest VARCHAR PRIMARY KEY, sha256 VARCHAR NOT NULL);
ALTER TABLE privacy_demo.batches ADD COLUMN IF NOT EXISTS loaded_at TIMESTAMPTZ;
ALTER TABLE privacy_demo.batches ADD COLUMN IF NOT EXISTS seed BIGINT;
ALTER TABLE privacy_demo.batches ADD COLUMN IF NOT EXISTS interval_seconds INTEGER;
ALTER TABLE privacy_demo.batches ADD COLUMN IF NOT EXISTS key_id VARCHAR;
CREATE TABLE IF NOT EXISTS privacy_demo.order_batches (
 manifest VARCHAR, dataset_id VARCHAR, order_id BIGINT,
 PRIMARY KEY(manifest,dataset_id,order_id));
CREATE TABLE IF NOT EXISTS privacy_demo.customer_batches (
 manifest VARCHAR, dataset_id VARCHAR, customer_token VARCHAR, as_of DATE,
 PRIMARY KEY(manifest,dataset_id,customer_token,as_of));
"""


def load_private(store: Store, manifest: str, database: Path) -> dict:
    if not manifest.startswith("customer-orders/") or not manifest.endswith("/metadata.json"):
        raise ValueError("Invalid private manifest path")
    metadata_bytes = store.read(manifest)
    metadata = json.loads(metadata_bytes)
    if set(metadata) != {
        "contract",
        "key_id",
        "seed",
        "interval_seconds",
        "as_of",
        "manifest_version",
        "files",
    }:
        raise ValueError("Invalid masked manifest")
    if metadata["manifest_version"] != 1 or set(metadata["files"]) != {
        "orders.csv",
        "customers.json",
    }:
        raise ValueError("Invalid masked manifest files")
    files = {}
    for name in ["orders.csv", "customers.json"]:
        payload = store.read(manifest.removesuffix("metadata.json") + name)
        if metadata["files"][name] != {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }:
            raise ValueError("Masked payload checksum mismatch")
        files[name] = payload.decode("utf-8")
    reader = csv.DictReader(io.StringIO(files["orders.csv"]))
    if reader.fieldnames != list(PrivateOrder.model_fields):
        raise ValueError("Masked order columns changed")
    stage = {
        key: value for key, value in metadata.items() if key not in {"manifest_version", "files"}
    }
    stage.update(orders=list(reader), customers=json.loads(files["customers.json"]))
    orders, customers = validate_safe(stage)
    if not database.is_file():
        raise FileNotFoundError("Refusing to initialize an unreviewed warehouse")
    dataset = hashlib.sha256(
        json.dumps(
            ["orders-v4", metadata["seed"], metadata["interval_seconds"], metadata["key_id"]]
        ).encode()
    ).hexdigest()
    checksum = hashlib.sha256(metadata_bytes).hexdigest()
    with FileLock(str(database) + ".lock", mode=0o660, timeout=60):
        with duckdb.connect(
            str(database), config={"threads": 1, "memory_limit": "256MB"}
        ) as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                connection.execute(SCHEMA)
                previous = connection.execute(
                    "SELECT sha256 FROM privacy_demo.batches WHERE manifest=?", [manifest]
                ).fetchone()
                if previous and previous[0] != checksum:
                    raise ValueError("Previously loaded masked manifest changed")
                if previous:
                    connection.execute("COMMIT")
                    return {"already_loaded": True, "orders": 0, "customers": 0}
                for row in orders:
                    record = (
                        dataset,
                        row.order_id,
                        row.created_at,
                        row.country,
                        row.currency,
                        row.product_id,
                        row.quantity,
                        row.unit_price_minor,
                        row.total_minor,
                        row.customer_token,
                    )
                    old = connection.execute(
                        "SELECT * FROM privacy_demo.orders WHERE dataset_id=? AND order_id=?",
                        [dataset, row.order_id],
                    ).fetchone()
                    if old is not None and old != record:
                        raise ValueError("Conflicting historical private order")
                    if old is None:
                        connection.execute(
                            "INSERT INTO privacy_demo.orders VALUES (?,?,?,?,?,?,?,?,?,?)", record
                        )
                    connection.execute(
                        "INSERT INTO privacy_demo.order_batches VALUES (?,?,?)",
                        [manifest, dataset, row.order_id],
                    )
                for row in customers:
                    record = (
                        dataset,
                        row.customer_token,
                        date.fromisoformat(metadata["as_of"]),
                        row.country,
                        row.age_band,
                        True,
                    )
                    old = connection.execute(
                        "SELECT * FROM privacy_demo.customers "
                        "WHERE dataset_id=? AND customer_token=? AND as_of=?",
                        record[:3],
                    ).fetchone()
                    if old is not None and old != record:
                        raise ValueError("Conflicting historical private customer snapshot")
                    if old is None:
                        connection.execute(
                            "INSERT INTO privacy_demo.customers VALUES (?,?,?,?,?,?)", record
                        )
                    connection.execute(
                        "INSERT INTO privacy_demo.customer_batches VALUES (?,?,?,?)",
                        [manifest, *record[:3]],
                    )
                connection.execute(
                    "INSERT INTO privacy_demo.batches "
                    "(manifest,sha256,loaded_at,seed,interval_seconds,key_id) VALUES (?,?,?,?,?,?)",
                    [
                        manifest,
                        checksum,
                        datetime.now(UTC),
                        metadata["seed"],
                        metadata["interval_seconds"],
                        metadata["key_id"],
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
    return {"already_loaded": False, "orders": len(orders), "customers": len(customers)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init-key")
    sub.add_parser("prepare-key-file")
    extract = sub.add_parser("extract")
    extract.add_argument("--run-id", default=None)
    extract.add_argument("--limit", type=int, default=20)
    extract.add_argument("--after-id", type=int)
    loading = sub.add_parser("load")
    loading.add_argument("--manifest", required=True)
    loading.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "init-key":
            initialize_key()
            print(json.dumps({"status": "privacy_key_ready"}))
            return
        if args.action == "prepare-key-file":
            prepare_key_file()
            print(json.dumps({"status": "scheduler_key_file_ready"}))
            return
        store = BlobStore()
        try:
            if args.action == "extract":
                key = PrivacyKey.from_environment()
                with httpx.Client(
                    base_url="http://127.0.0.1:18000",
                    timeout=15,
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    manifest = extract_private(
                        store,
                        client,
                        Path("artifacts/customer-ingestion"),
                        args.run_id or uuid4().hex,
                        key,
                        after_id=args.after_id,
                        limit=args.limit,
                    )
                print(json.dumps({"status": "masked_batch_landed", "manifest": manifest}))
            else:
                print(json.dumps(load_private(store, args.manifest, args.database)))
        finally:
            store.close()
    except Exception as error:
        print(
            json.dumps({"status": "customer_pipeline_failed", "error_type": type(error).__name__})
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
