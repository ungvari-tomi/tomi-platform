"""Validate and load bounded products-v1 snapshots without a MongoDB driver."""

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
from filelock import FileLock

from retailflow.batch_landing import Store
from retailflow.blob_storage import BlobStore
from retailflow.currencies import COUNTRY_PROFILES
from retailflow.warehouse import DDL as WAREHOUSE_DDL
from retailflow.warehouse import aware_datetime

SOURCE = "product-catalog/products-v1"
DDL = """
CREATE TABLE IF NOT EXISTS raw.product_versions (
    product_id VARCHAR NOT NULL, revision INTEGER NOT NULL CHECK (revision > 0),
    schema_version INTEGER NOT NULL, sku VARCHAR NOT NULL, name VARCHAR NOT NULL,
    category VARCHAR NOT NULL, status VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
    document_json VARCHAR NOT NULL, document_sha256 VARCHAR NOT NULL,
    first_manifest VARCHAR NOT NULL,
    PRIMARY KEY (product_id, revision)
);
CREATE TABLE IF NOT EXISTS ops.product_batch_records (
    manifest_path VARCHAR NOT NULL, product_id VARCHAR NOT NULL, revision INTEGER NOT NULL,
    PRIMARY KEY (manifest_path, product_id)
);
"""


def unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def integer(value: object) -> int:
    if not isinstance(value, dict) or set(value) != {"$numberInt"}:
        raise ValueError("Expected canonical BSON int32")
    encoded = value["$numberInt"]
    if not isinstance(encoded, str) or not re.fullmatch(r"0|-?[1-9][0-9]{0,9}", encoded):
        raise ValueError("Invalid canonical int32")
    result = int(encoded)
    if not -(2**31) <= result < 2**31:
        raise ValueError("BSON int32 out of range")
    return result


def timestamp(value: object) -> datetime:
    if (
        not isinstance(value, dict)
        or set(value) != {"$date"}
        or not isinstance(value["$date"], dict)
        or set(value["$date"]) != {"$numberLong"}
    ):
        raise ValueError("Expected canonical BSON UTC date")
    milliseconds = value["$date"]["$numberLong"]
    if not isinstance(milliseconds, str) or not re.fullmatch(r"0|-?[1-9][0-9]{0,18}", milliseconds):
        raise ValueError("Invalid BSON date milliseconds")
    try:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(milliseconds))
    except OverflowError:
        raise ValueError("BSON date exceeds warehouse range") from None


def parse_document(row: dict) -> tuple:
    fields = {
        "_id",
        "sku",
        "name",
        "category",
        "attributes",
        "variants",
        "markets",
        "schema_version",
        "revision",
        "synthetic",
        "classification",
        "status",
        "created_at",
        "updated_at",
    }
    if not isinstance(row, dict) or set(row) != fields:
        raise ValueError("Product fields differ from products-v1")
    identifiers = {f"product-{i:03d}" for i in range(1, 21)}
    skus = {f"RF-{i:04d}" for i in range(1, 21)}
    if (
        row["_id"] not in identifiers
        or row["sku"] not in skus
        or row["synthetic"] is not True
        or row["classification"] != "synthetic_product_metadata"
        or integer(row["schema_version"]) != 1
        or row["status"] not in {"active", "retired"}
        or not isinstance(row["name"], str)
        or not 1 <= len(row["name"]) <= 120
    ):
        raise ValueError("Invalid product identity, classification or version")
    revision = integer(row["revision"])
    created, updated = timestamp(row["created_at"]), timestamp(row["updated_at"])
    if revision < 1 or updated < created:
        raise ValueError("Invalid product revision or date ordering")
    attributes = row["attributes"]
    if not isinstance(attributes, dict):
        raise ValueError("Product attributes must be an object")
    category = row["category"]
    if category == "electronics":
        valid = set(attributes) == {"power_watts", "warranty_months"}
        valid = valid and 1 <= integer(attributes["power_watts"]) <= 500
        valid = valid and integer(attributes["warranty_months"]) in {12, 24, 36}
    elif category == "apparel":
        valid = set(attributes) == {"fabric", "care_temperature_c"}
        valid = valid and attributes["fabric"] in {"cotton", "linen"}
        valid = valid and integer(attributes["care_temperature_c"]) in {30, 40}
    elif category == "homeware":
        valid = set(attributes) == {"material", "dishwasher_safe"}
        valid = valid and attributes["material"] in {"ceramic", "glass"}
        valid = valid and type(attributes["dishwasher_safe"]) is bool
    else:
        valid = False
    if not valid:
        raise ValueError("Invalid category-specific attributes")
    markets, variants = row["markets"], row["variants"]
    if (
        not isinstance(markets, list)
        or not 1 <= len(markets) <= 11
        or any(not isinstance(m, str) or m not in COUNTRY_PROFILES for m in markets)
        or len(set(markets)) != len(markets)
        or not isinstance(variants, list)
        or not 1 <= len(variants) <= 2
    ):
        raise ValueError("Invalid markets or variants")
    variant_skus = []
    for variant in variants:
        if (
            not isinstance(variant, dict)
            or set(variant) != {"sku", "option"}
            or variant["sku"] not in {sku + suffix for sku in skus for suffix in ("-A", "-B")}
            or variant["option"]
            not in ({"M", "L"} if category == "apparel" else {"black", "white"})
        ):
            raise ValueError("Invalid category variant")
        variant_skus.append(variant["sku"])
    if len(set(variant_skus)) != len(variants):
        raise ValueError("Duplicate variant SKU")
    canonical = json.dumps(row, sort_keys=True)
    return (
        row["_id"],
        revision,
        1,
        row["sku"],
        row["name"],
        category,
        row["status"],
        created,
        updated,
        canonical,
        hashlib.sha256(canonical.encode()).hexdigest(),
    )


def validate(store: Store, manifest: str) -> tuple[dict, list[tuple], str]:
    if not re.fullmatch(
        SOURCE + r"/ingestion_date=[0-9]{4}-[0-9]{2}-[0-9]{2}/batch=[a-f0-9]{32}/metadata[.]json",
        manifest,
    ):
        raise ValueError("Unapproved product manifest path")
    encoded = store.read(manifest)
    metadata = json.loads(encoded, object_pairs_hook=unique_object)
    if (
        type(metadata.get("manifest_version")) is not int
        or metadata["manifest_version"] != 1
        or metadata.get("source") != SOURCE
        or metadata.get("format") != "json"
        or metadata.get("encoding") != "utf-8"
        or metadata.get("payload_blob") != manifest.removesuffix("metadata.json") + "data.json"
        or metadata.get("batch_id") != manifest.split("/batch=")[1].split("/")[0]
    ):
        raise ValueError("Product manifest contract mismatch")
    observed = aware_datetime(metadata["retrieved_at"])
    if observed.date().isoformat() != manifest.split("ingestion_date=")[1].split("/")[0]:
        raise ValueError("Product ingestion date mismatch")
    provenance = metadata["source_metadata"]
    if (
        provenance.get("dataset_version") != "products-v1"
        or provenance.get("representation") != "mongodb_canonical_extended_json"
        or provenance.get("classification") != "synthetic_product_metadata"
        or type(provenance.get("row_count")) is not int
        or provenance["row_count"] != 20
        or type(provenance.get("sequence")) is not int
        or provenance["sequence"] < 1
        or metadata.get("request")
        != {"database": "retailflow", "collection": "products", "mode": "full_reconcile"}
    ):
        raise ValueError("Invalid product provenance")
    payload = store.read(metadata["payload_blob"])
    if (
        type(metadata.get("bytes")) is not int
        or len(payload) != metadata["bytes"]
        or hashlib.sha256(payload).hexdigest() != metadata.get("sha256")
    ):
        raise ValueError("Product payload checksum mismatch")
    documents = json.loads(payload, object_pairs_hook=unique_object)
    if not isinstance(documents, list) or len(documents) != 20:
        raise ValueError("Product snapshot must have twenty documents")
    rows = [parse_document(row) for row in documents]
    if len({r[0] for r in rows}) != 20 or len({r[3] for r in rows}) != 20:
        raise ValueError("Duplicate product identity or SKU")
    variants = [v["sku"] for doc in documents for v in doc["variants"]]
    if len(set(variants)) != len(variants):
        raise ValueError("Variant SKU reused across products")
    return metadata, rows, hashlib.sha256(encoded).hexdigest()


def load_manifest(store: Store, manifest: str, database: Path, *, lock_timeout: float = 60) -> dict:
    metadata, rows, digest = validate(store, manifest)
    if not database.is_file():
        raise FileNotFoundError("Product loading requires an initialized warehouse")
    with FileLock(str(database) + ".lock", mode=0o660, timeout=lock_timeout):
        connection = duckdb.connect(str(database), config={"threads": 1, "memory_limit": "256MB"})
        try:
            connection.execute("BEGIN TRANSACTION")
            connection.execute(WAREHOUSE_DDL)
            if connection.execute("SELECT version FROM ops.schema_version").fetchall() != [(1,)]:
                raise ValueError("Unsupported warehouse schema version")
            connection.execute(DDL)
            previous = connection.execute(
                "SELECT manifest_sha256 FROM ops.loaded_batches WHERE manifest_path = ?", [manifest]
            ).fetchone()
            if previous:
                if previous[0] != digest:
                    raise ValueError("Previously loaded product manifest changed")
                connection.execute("COMMIT")
                return {"already_loaded": True, "rows_inserted": 0, "manifest": manifest}
            inserted = 0
            for row in rows:
                old = connection.execute(
                    "SELECT document_sha256 FROM raw.product_versions "
                    "WHERE product_id=? AND revision=?",
                    row[:2],
                ).fetchone()
                if old and old[0] != row[10]:
                    raise ValueError("Conflicting historical product revision")
                inconsistent = connection.execute(
                    """SELECT COUNT(*) FROM raw.product_versions WHERE product_id=? AND (
                        created_at <> ? OR (revision < ? AND updated_at >= ?)
                        OR (revision > ? AND updated_at <= ?))""",
                    [row[0], row[7], row[1], row[8], row[1], row[8]],
                ).fetchone()[0]
                if inconsistent:
                    raise ValueError("Product revision timestamps or creation identity conflict")
                if old is None:
                    connection.execute(
                        "INSERT INTO raw.product_versions "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [*row, manifest],
                    )
                    inserted += 1
                connection.execute(
                    "INSERT INTO ops.product_batch_records VALUES (?, ?, ?)", [manifest, *row[:2]]
                )
            connection.execute(
                "INSERT INTO ops.loaded_batches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    manifest,
                    digest,
                    metadata["sha256"],
                    SOURCE,
                    aware_datetime(metadata["retrieved_at"]),
                    datetime.now(UTC),
                    20,
                    inserted,
                    json.dumps(metadata, sort_keys=True),
                ],
            )
            connection.execute("COMMIT")
            return {"already_loaded": False, "rows_inserted": inserted, "manifest": manifest}
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    store = None
    try:
        store = BlobStore()
        print(json.dumps(load_manifest(store, args.manifest, args.database)))
    except Exception as error:
        raise SystemExit(
            f"Product load failed ({type(error).__name__}); details withheld"
        ) from None
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    main()
