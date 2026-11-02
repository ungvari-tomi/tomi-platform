"""Transactional, replay-safe loading of verified landing batches into DuckDB."""

import argparse
import csv
import hashlib
import io
import json
import os
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
from filelock import FileLock

from retailflow.api import Order
from retailflow.blob_storage import BlobStore
from retailflow.currencies import FX_QUOTES, SUPPORTED_DATASETS, valid_price
from retailflow.landing import Store

DDL = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE TABLE IF NOT EXISTS ops.schema_version (version INTEGER PRIMARY KEY);
INSERT INTO ops.schema_version VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS ops.datasets (
    dataset_id VARCHAR PRIMARY KEY, dataset_version VARCHAR NOT NULL,
    seed BIGINT NOT NULL, interval_seconds INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS raw.orders (
    dataset_id VARCHAR NOT NULL, order_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL, country VARCHAR NOT NULL,
    currency VARCHAR NOT NULL, product_id VARCHAR NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_minor BIGINT NOT NULL CHECK (unit_price_minor > 0),
    total_minor BIGINT NOT NULL CHECK (total_minor = quantity * unit_price_minor),
    first_manifest VARCHAR NOT NULL,
    PRIMARY KEY (dataset_id, order_id)
);
CREATE TABLE IF NOT EXISTS raw.exchange_rates (
    provider VARCHAR NOT NULL, base VARCHAR NOT NULL, quote VARCHAR NOT NULL,
    rate_date DATE NOT NULL, rate DECIMAL(24,12) NOT NULL CHECK (rate > 0),
    first_manifest VARCHAR NOT NULL, PRIMARY KEY (provider, base, quote, rate_date)
);
CREATE TABLE IF NOT EXISTS ops.loaded_batches (
    manifest_path VARCHAR PRIMARY KEY, manifest_sha256 VARCHAR NOT NULL,
    payload_sha256 VARCHAR NOT NULL, source VARCHAR NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL, loaded_at TIMESTAMPTZ NOT NULL,
    rows_seen INTEGER NOT NULL, rows_inserted INTEGER NOT NULL,
    metadata_json VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS ops.order_batch_records (
    manifest_path VARCHAR NOT NULL, dataset_id VARCHAR NOT NULL, order_id BIGINT NOT NULL,
    PRIMARY KEY (manifest_path, dataset_id, order_id)
);
CREATE TABLE IF NOT EXISTS ops.fx_batch_records (
    manifest_path VARCHAR NOT NULL, provider VARCHAR NOT NULL, base VARCHAR NOT NULL,
    quote VARCHAR NOT NULL, rate_date DATE NOT NULL, requested_date DATE NOT NULL,
    PRIMARY KEY (manifest_path, provider, base, quote, rate_date)
);
"""


def aware_datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return result.astimezone(UTC)


def validate_batch(store: Store, manifest: str) -> tuple[dict, bytes, str]:
    encoded = store.read(manifest)
    metadata = json.loads(encoded)
    source = metadata.get("source")
    extension = {"orders": "csv", "exchange-rates": "json"}.get(source)
    if extension is None or metadata.get("manifest_version") != 1:
        raise ValueError("Unsupported source or manifest version")
    if not manifest.startswith(source + "/") or not manifest.endswith("/metadata.json"):
        raise ValueError("Unexpected manifest path")
    expected = manifest.rsplit("/", 1)[0] + f"/data.{extension}"
    if metadata.get("payload_blob") != expected or metadata.get("format") != extension:
        raise ValueError("Manifest payload path/format mismatch")
    if metadata.get("encoding") != "utf-8":
        raise ValueError("Unsupported payload encoding")
    digest = metadata.get("sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid payload checksum")
    payload = store.read(expected)
    if len(payload) != metadata.get("bytes") or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("Payload checksum or byte count mismatch")
    aware_datetime(metadata["retrieved_at"])
    return metadata, payload, hashlib.sha256(encoded).hexdigest()


def parse_orders(metadata: dict, payload: bytes) -> tuple[dict, list[tuple]]:
    source = metadata["source_metadata"]
    if source.get("dataset_version") not in SUPPORTED_DATASETS:
        raise ValueError("Unsupported orders dataset version")
    if type(source.get("seed")) is not int or type(source.get("interval_seconds")) is not int:
        raise ValueError("Dataset seed and interval must be integers")
    if not 1 <= source["interval_seconds"] <= 3600:
        raise ValueError("Invalid dataset interval")
    identity = {key: source[key] for key in ("dataset_version", "seed", "interval_seconds")}
    identity["dataset_id"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode()
    ).hexdigest()
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8"), newline=""), strict=True)
    if reader.fieldnames != list(Order.model_fields):
        raise ValueError("CSV header differs from the orders contract")
    rows = []
    seen = set()
    for item in reader:
        if set(item) != set(Order.model_fields) or any(value is None for value in item.values()):
            raise ValueError("CSV row width differs from its header")
        for field in ("order_id", "quantity", "unit_price_minor", "total_minor"):
            if not re.fullmatch(r"[0-9]+", item[field]):
                raise ValueError("CSV numeric fields must use integer minor units/counts")
        order = Order.model_validate(item)
        if not order.product_id.strip():
            raise ValueError("Product ID must not be empty")
        if order.created_at.utcoffset() is None:
            raise ValueError("Order timestamp must include a timezone")
        if order.order_id in seen:
            raise ValueError("Duplicate order ID within a batch")
        seen.add(order.order_id)
        if order.total_minor != order.quantity * order.unit_price_minor:
            raise ValueError("Invalid order total")
        if not valid_price(
            order.country, order.currency, order.unit_price_minor, source["dataset_version"]
        ):
            raise ValueError("Country/currency or versioned price contract mismatch")
        rows.append(
            (
                identity["dataset_id"],
                order.order_id,
                order.created_at.astimezone(UTC),
                order.country,
                order.currency,
                order.product_id,
                order.quantity,
                order.unit_price_minor,
                order.total_minor,
            )
        )
    if type(source.get("row_count")) is not int or len(rows) != source["row_count"]:
        raise ValueError("CSV row count differs from manifest")
    return identity, rows


def parse_rate(metadata: dict, payload: bytes) -> tuple:
    rows = json.loads(payload, parse_float=Decimal)
    source = metadata["source_metadata"]
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError("Expected one EUR-base reference observation")
    row = rows[0]
    if set(row) != {"date", "base", "quote", "rate"}:
        raise ValueError("Exchange-rate schema changed")
    if row["base"] != "EUR" or row["quote"] not in FX_QUOTES:
        raise ValueError("Unsupported currency pair")
    if source.get("provider") != "ECB" or source.get("source") != "Frankfurter":
        raise ValueError("Unexpected FX provenance")
    if isinstance(row["rate"], bool):
        raise ValueError("Boolean rate is invalid")
    rate = Decimal(str(row["rate"]))
    if not rate.is_finite() or not 0 < rate < Decimal("1000000000000"):
        raise ValueError("Rate must be finite, positive, and fit DECIMAL(24,12)")
    if rate != rate.quantize(Decimal("0.000000000001")):
        raise ValueError("Rate exceeds supported precision")
    published, requested = (
        date.fromisoformat(row["date"]),
        date.fromisoformat(source["requested_date"]),
    )
    if published > requested or published.isoformat() != source["rate_date"]:
        raise ValueError("FX publication/request dates conflict")
    if (
        rate != Decimal(source["rate"])
        or source.get("base") != "EUR"
        or source.get("quote") != row["quote"]
    ):
        raise ValueError("FX payload and metadata differ")
    return ("ECB", "EUR", row["quote"], published, rate, requested)


def load_manifest(store: Store, manifest: str, database: Path, *, lock_timeout: float = 60) -> dict:
    metadata, payload, manifest_hash = validate_batch(store, manifest)
    source = metadata["source"]
    identity, orders, rate = None, [], None
    if source == "orders":
        identity, orders = parse_orders(metadata, payload)
    else:
        rate = parse_rate(metadata, payload)
    database.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(database) + ".lock", mode=0o660, timeout=lock_timeout):
        connection = duckdb.connect(str(database), config={"threads": 1, "memory_limit": "256MB"})
        try:
            connection.execute("BEGIN TRANSACTION")
            connection.execute(DDL)
            if connection.execute("SELECT version FROM ops.schema_version").fetchall() != [(1,)]:
                raise ValueError("Unsupported warehouse schema version")
            previous = connection.execute(
                "SELECT manifest_sha256 FROM ops.loaded_batches WHERE manifest_path = ?", [manifest]
            ).fetchone()
            if previous:
                if previous[0] != manifest_hash:
                    raise ValueError("Previously loaded manifest changed")
                connection.execute("COMMIT")
                return {"manifest": manifest, "already_loaded": True, "rows_inserted": 0}
            inserted = 0
            if identity:
                connection.execute(
                    "INSERT INTO ops.datasets VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    [
                        identity[key]
                        for key in ("dataset_id", "dataset_version", "seed", "interval_seconds")
                    ],
                )
                for row in orders:
                    old = connection.execute(
                        """SELECT dataset_id, order_id, created_at, country, currency,
                                  product_id, quantity, unit_price_minor, total_minor
                           FROM raw.orders WHERE dataset_id = ? AND order_id = ?""",
                        row[:2],
                    ).fetchone()
                    if old is not None and old != row:
                        raise ValueError(
                            "Conflicting historical order; explicit correction policy required"
                        )
                    if old is None:
                        connection.execute(
                            "INSERT INTO raw.orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [*row, manifest],
                        )
                        inserted += 1
                    connection.execute(
                        "INSERT INTO ops.order_batch_records VALUES (?, ?, ?)", [manifest, *row[:2]]
                    )
            else:
                old = connection.execute(
                    """SELECT rate FROM raw.exchange_rates
                       WHERE provider = ? AND base = ? AND quote = ? AND rate_date = ?""",
                    rate[:4],
                ).fetchone()
                if old is not None and old[0] != rate[4]:
                    raise ValueError(
                        "Conflicting published FX rate; explicit revision policy required"
                    )
                if old is None:
                    connection.execute(
                        "INSERT INTO raw.exchange_rates VALUES (?, ?, ?, ?, ?, ?)",
                        [*rate[:5], manifest],
                    )
                    inserted = 1
                connection.execute(
                    "INSERT INTO ops.fx_batch_records VALUES (?, ?, ?, ?, ?, ?)",
                    [manifest, *rate[:4], rate[5]],
                )
            connection.execute(
                "INSERT INTO ops.loaded_batches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    manifest,
                    manifest_hash,
                    metadata["sha256"],
                    source,
                    aware_datetime(metadata["retrieved_at"]),
                    datetime.now(UTC),
                    len(orders) if identity else 1,
                    inserted,
                    json.dumps(metadata, sort_keys=True),
                ],
            )
            connection.execute("COMMIT")
            return {"manifest": manifest, "already_loaded": False, "rows_inserted": inserted}
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


def load_source(store: BlobStore, database: Path, source: str) -> dict:
    if source not in {"orders", "exchange-rates"}:
        raise ValueError("Unsupported warehouse source")
    manifests = sorted(
        blob.name
        for blob in store.container.list_blobs(name_starts_with=source + "/")
        if blob.name.endswith("/metadata.json")
    )
    loaded = skipped = inserted = 0
    for manifest in manifests:
        result = load_manifest(store, manifest, database)
        skipped += int(result["already_loaded"])
        loaded += int(not result["already_loaded"])
        inserted += result["rows_inserted"]
    return {
        "source": source,
        "batches_loaded": loaded,
        "batches_skipped": skipped,
        "rows_inserted": inserted,
    }


def scheduled_load(source: str) -> dict:
    store = BlobStore()
    try:
        return load_source(store, Path(os.environ["RETAILFLOW_DUCKDB_PATH"]), source)
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--source", choices=["orders", "exchange-rates", "all"], default="all")
    args = parser.parse_args()
    store = BlobStore()
    try:
        for source in ["orders", "exchange-rates"] if args.source == "all" else [args.source]:
            print(json.dumps(load_source(store, args.database, source)))
    finally:
        store.close()


if __name__ == "__main__":
    main()
