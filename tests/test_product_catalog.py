"""Product versions, validated BSON contracts, atomic lineage and replay boundaries."""

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import duckdb
import pytest
from filelock import FileLock, Timeout
from test_landing import MemoryStore

from infra.mongodb.catalog import documents
from infra.mongodb.extract import encode
from retailflow.batch_landing import land
from retailflow.product_catalog import SOURCE, load_manifest, validate
from retailflow.warehouse import DDL as WAREHOUSE_DDL


def snapshot(store, rows=None, *, observed=None):
    return land(
        store,
        source=SOURCE,
        payload=encode(documents() if rows is None else rows),
        extension="json",
        batch_id=uuid4().hex,
        retrieved_at=observed or datetime.now(UTC),
        request={"database": "retailflow", "collection": "products", "mode": "full_reconcile"},
        source_metadata={
            "dataset_version": "products-v1",
            "row_count": 20,
            "sequence": 1,
            "representation": "mongodb_canonical_extended_json",
            "classification": "synthetic_product_metadata",
        },
    )


@pytest.fixture
def warehouse(tmp_path):
    database = tmp_path / "retailflow.duckdb"
    with FileLock(str(database) + ".lock", mode=0o660):
        with duckdb.connect(str(database)) as connection:
            connection.execute(WAREHOUSE_DDL)
    return database


def counts(database):
    with FileLock(str(database) + ".lock", mode=0o660):
        with duckdb.connect(str(database), read_only=True) as connection:
            return tuple(
                connection.execute(sql).fetchone()[0]
                for sql in [
                    "SELECT COUNT(*) FROM raw.product_versions",
                    "SELECT COUNT(*) FROM ops.product_batch_records",
                    "SELECT COUNT(*) FROM ops.loaded_batches",
                ]
            )


def test_snapshot_replay_and_new_revision_preserve_history_and_membership(warehouse):
    store = MemoryStore()
    first = snapshot(store)
    assert load_manifest(store, first, warehouse)["rows_inserted"] == 20
    assert load_manifest(store, first, warehouse)["already_loaded"]
    assert counts(warehouse) == (20, 20, 1)
    rows = documents()
    rows[0].update(
        revision=2, status="retired", updated_at=rows[0]["updated_at"] + timedelta(days=1)
    )
    assert load_manifest(store, snapshot(store, rows), warehouse)["rows_inserted"] == 1
    assert counts(warehouse) == (21, 40, 2)
    with FileLock(str(warehouse) + ".lock", mode=0o660):
        with duckdb.connect(str(warehouse), read_only=True) as connection:
            assert connection.execute("SELECT COUNT(*) FROM raw.orders").fetchone() == (0,)
            assert connection.execute(
                "SELECT status FROM raw.product_versions "
                "WHERE product_id='product-001' ORDER BY revision"
            ).fetchall() == [("active",), ("retired",)]


def test_late_conflict_rolls_back_earlier_inserts_memberships_and_ledger(warehouse):
    store = MemoryStore()
    load_manifest(store, snapshot(store), warehouse)
    rows = documents()
    rows[0].update(revision=2, updated_at=rows[0]["updated_at"] + timedelta(days=1))
    rows[-1]["name"] = "Conflicting historical name"
    with pytest.raises(ValueError, match="historical"):
        load_manifest(store, snapshot(store, rows), warehouse)
    assert counts(warehouse) == (20, 20, 1)


def test_changed_manifest_and_corrupt_bytes_are_not_replays(warehouse):
    store = MemoryStore()
    manifest = snapshot(store)
    load_manifest(store, manifest, warehouse)
    metadata = json.loads(store.blobs[manifest])
    metadata["source_metadata"]["sequence"] += 1
    store.blobs[manifest] = json.dumps(metadata).encode()
    with pytest.raises(ValueError, match="manifest changed"):
        load_manifest(store, manifest, warehouse)
    store.blobs[metadata["payload_blob"]] += b"corrupt"
    with pytest.raises(ValueError, match="checksum"):
        load_manifest(store, manifest, warehouse)
    assert counts(warehouse) == (20, 20, 1)


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "duplicate",
        "field",
        "version",
        "revision_bool",
        "negative_revision",
        "classification",
        "date_order",
        "markets",
        "variant",
        "attributes",
        "sku",
    ],
)
def test_bad_source_contracts_never_enter_the_warehouse(warehouse, case):
    store = MemoryStore()
    rows = copy.deepcopy(documents())
    if case == "missing":
        rows.pop()
    elif case == "duplicate":
        rows[-1] = rows[0]
    elif case == "field":
        rows[0]["email"] = "synthetic@example.test"
    elif case == "version":
        rows[0]["schema_version"] = 2
    elif case == "revision_bool":
        rows[0]["revision"] = True
    elif case == "negative_revision":
        rows[0]["revision"] = -1
    elif case == "classification":
        rows[0]["synthetic"] = False
    elif case == "date_order":
        rows[0]["updated_at"] -= timedelta(days=1)
    elif case == "markets":
        rows[0]["markets"] = ["HU", "HU"]
    elif case == "variant":
        rows[0]["variants"] *= 2
    elif case == "attributes":
        rows[0]["attributes"]["power_watts"] = 501
    else:
        rows[1]["sku"] = rows[0]["sku"]
    with pytest.raises(ValueError):
        load_manifest(store, snapshot(store, rows), warehouse)
    with FileLock(str(warehouse) + ".lock", mode=0o660):
        with duckdb.connect(str(warehouse), read_only=True) as connection:
            assert connection.execute("SELECT COUNT(*) FROM ops.loaded_batches").fetchone() == (0,)
            assert connection.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='product_versions'"
            ).fetchone() == (0,)


def test_loader_obeys_common_lock_and_refuses_missing_warehouse(warehouse, tmp_path):
    store = MemoryStore()
    manifest = snapshot(store)
    with FileLock(str(warehouse) + ".lock", mode=0o660):
        with pytest.raises(Timeout):
            load_manifest(store, manifest, warehouse, lock_timeout=0)
    missing = tmp_path / "missing.duckdb"
    with pytest.raises(FileNotFoundError):
        load_manifest(store, manifest, missing)
    assert not missing.exists()


def test_out_of_order_snapshots_and_invalid_revision_clock(warehouse):
    store = MemoryStore()
    rows = documents()
    rows[0].update(revision=2, updated_at=rows[0]["updated_at"] + timedelta(days=1))
    load_manifest(store, snapshot(store, rows), warehouse)
    assert load_manifest(store, snapshot(store), warehouse)["rows_inserted"] == 1
    rows[0].update(revision=3, updated_at=rows[0]["updated_at"] - timedelta(hours=1))
    with pytest.raises(ValueError, match="timestamps"):
        load_manifest(store, snapshot(store, rows), warehouse)
    assert counts(warehouse) == (21, 40, 2)


def test_manifest_namespace_and_provenance_are_enforced():
    store = MemoryStore()
    manifest = snapshot(store)
    with pytest.raises(ValueError, match="path"):
        validate(store, "orders/metadata.json")
    metadata = json.loads(store.blobs[manifest])
    metadata["request"]["database"] = "other"
    store.blobs[manifest] = json.dumps(metadata).encode()
    with pytest.raises(ValueError, match="provenance"):
        validate(store, manifest)


def test_duplicate_json_fields_cannot_hide_a_contract_change(warehouse):
    store = MemoryStore()
    manifest = snapshot(store)
    metadata = json.loads(store.blobs[manifest])
    name = metadata["payload_blob"]
    payload = store.blobs[name].replace(
        b'"_id": "product-001"', b'"_id": "product-001", "_id": "product-001"', 1
    )
    metadata.update(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    store.blobs[name], store.blobs[manifest] = payload, json.dumps(metadata).encode()
    with pytest.raises(ValueError, match="Duplicate JSON"):
        load_manifest(store, manifest, warehouse)


def test_legal_category_variants_and_canonical_hashes_are_preserved(warehouse):
    store = MemoryStore()
    rows = documents()
    rows[0]["name"] = "Synthetic méret"
    rows[0]["attributes"].update(power_watts=500, warranty_months=36)
    rows[1]["attributes"].update(fabric="linen", care_temperature_c=40)
    rows[2]["attributes"].update(material="glass", dishwasher_safe=False)
    rows[1]["markets"], rows[1]["variants"] = ["HU"], rows[1]["variants"][:1]
    manifest = snapshot(store, rows)
    load_manifest(store, manifest, warehouse)
    with FileLock(str(warehouse) + ".lock", mode=0o660):
        with duckdb.connect(str(warehouse), read_only=True) as connection:
            value, digest = connection.execute(
                "SELECT document_json,document_sha256 FROM raw.product_versions "
                "WHERE product_id='product-001'"
            ).fetchone()
    assert value.encode() == encode(rows[0])
    assert digest == hashlib.sha256(encode(rows[0])).hexdigest()
