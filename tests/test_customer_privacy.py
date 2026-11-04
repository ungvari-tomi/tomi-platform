"""Privacy boundaries use disposable storage and an in-process synthetic source."""

import base64
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime

import duckdb
import pytest
from fastapi.testclient import TestClient
from filelock import FileLock, Timeout

from retailflow import customer_ingestion
from retailflow.api import Settings, create_app
from retailflow.customer_ingestion import extract_private, land_private, load_private, retrieve
from retailflow.privacy import PrivacyKey


class MemoryStore:
    def __init__(self):
        self.files = {}
        self.fail_manifest_once = False

    def put(self, name, payload, content_type):
        if name in self.files and self.files[name] != payload:
            raise ValueError("Immutable content changed")
        self.files[name] = payload
        if self.fail_manifest_once and name.endswith("metadata.json"):
            self.fail_manifest_once = False
            raise OSError("Simulated lost acknowledgement")

    def read(self, name):
        return self.files[name]


@pytest.fixture
def client():
    now = datetime(2026, 11, 4, tzinfo=UTC)
    with TestClient(create_app(Settings(), clock=lambda: now)) as source:
        yield source


@pytest.fixture
def key():
    return PrivacyKey(bytes(range(32)))  # Public test fixture, never a runtime key.


def test_source_customers_are_exactly_referenced_and_legacy_is_unchanged(client):
    orders = client.get("/v2/orders?after_id=0&limit=100").json()
    assert orders["dataset_version"] == "orders-v4"
    refs = {row["customer_id"] for row in orders["orders"]}
    customers = client.get(
        "/v2/customers", params=[("order_ids", row["order_id"]) for row in orders["orders"]]
    ).json()
    assert {row["customer_id"] for row in customers["customers"]} == refs
    assert len(customers["customers"]) == len(refs)
    assert all(row["email"].endswith("@example.invalid") for row in customers["customers"])
    assert client.get("/v2/customers").status_code == 422
    assert client.get("/v2/customers?order_ids=0").status_code == 422
    assert client.get("/v2/customers?order_ids=999999999").status_code == 422
    legacy = client.get("/v1/orders?after_id=0&limit=100").json()
    assert legacy["dataset_version"] == "orders-v3"
    for original, linked in zip(legacy["orders"], orders["orders"], strict=True):
        assert linked == {**original, "customer_id": linked["customer_id"]}


def test_tokens_join_and_no_direct_details_are_staged_or_landed(client, key, tmp_path):
    store = MemoryStore()
    manifest = extract_private(store, client, tmp_path, "first", key, after_id=0, limit=100)
    persisted = b"\n".join(store.files.values()) + next(tmp_path.glob("*.json")).read_bytes()
    for forbidden in [
        b"customer_id",
        b"first_name",
        b"last_name",
        b"email",
        b"phone",
        b"address",
        b"birth_date",
        b"Demo",
        b"example.invalid",
        b"customer-42-",
    ]:
        assert forbidden not in persisted
    assert key.value not in persisted
    stage = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert {r["customer_token"] for r in stage["orders"]} == {
        r["customer_token"] for r in stage["customers"]
    }
    assert store.files[manifest]
    assert key.token("same") == key.token("same")
    assert key.token("same") != PrivacyKey(bytes(reversed(range(32)))).token("same")
    assert str(key.value) not in repr(key)


def test_retry_replays_only_masked_content_without_source_calls(client, key, tmp_path):
    store = MemoryStore()
    store.fail_manifest_once = True
    with pytest.raises(OSError):
        extract_private(store, client, tmp_path, "retry", key, after_id=0)
    before = dict(store.files)

    class NoCalls:
        def get(self, *args, **kwargs):
            pytest.fail("Retry must use the frozen masked stage")

    extract_private(store, NoCalls(), tmp_path, "retry", key)
    assert store.files == before
    with pytest.raises(ValueError, match="key ID"):
        extract_private(store, NoCalls(), tmp_path, "retry", PrivacyKey(key.value, "next"))


@pytest.mark.parametrize("mutation", ["extra", "missing", "duplicate", "pii", "country"])
def test_minimal_contract_rejects_inconsistent_or_identifying_rows(client, key, mutation):
    stage = retrieve(client, key, after_id=0, limit=20)
    if mutation == "extra":
        row = deepcopy(stage["customers"][0])
        row["customer_token"] = key.token("unreferenced")
        stage["customers"].append(row)
    elif mutation == "missing":
        stage["customers"].pop()
    elif mutation == "duplicate":
        stage["customers"].append(deepcopy(stage["customers"][0]))
    elif mutation == "country":
        stage["customers"][0]["country"] = "ZZ"
    else:
        stage["customers"][0]["email"] = "DO-NOT-LEAK@example.invalid"
    store = MemoryStore()
    with pytest.raises(ValueError) as failure:
        land_private(store, stage, "a" * 32)
    assert "DO-NOT-LEAK" not in str(failure.value)
    assert not store.files


def test_loader_idempotence_checksums_and_atomic_conflict(client, key, tmp_path):
    store = MemoryStore()
    stage = retrieve(client, key, after_id=0, limit=20)
    manifest = land_private(store, stage, "a" * 32)
    database = tmp_path / "retailflow.duckdb"
    duckdb.connect(str(database)).close()
    assert load_private(store, manifest, database)["orders"] == 20
    assert load_private(store, manifest, database)["already_loaded"]
    with duckdb.connect(str(database)) as db:
        assert db.execute("SELECT count(*) FROM privacy_demo.orders").fetchone() == (20,)
        assert db.execute(
            "SELECT count(*) FROM privacy_demo.customers c WHERE NOT EXISTS "
            "(SELECT 1 FROM privacy_demo.orders o WHERE o.dataset_id=c.dataset_id "
            "AND o.customer_token=c.customer_token)"
        ).fetchone() == (0,)
    changed = deepcopy(stage)
    changed["orders"][0]["quantity"] = 1 if stage["orders"][0]["quantity"] != 1 else 2
    changed["orders"][0]["total_minor"] = (
        changed["orders"][0]["quantity"] * changed["orders"][0]["unit_price_minor"]
    )
    conflicting = land_private(store, changed, "b" * 32)
    with pytest.raises(ValueError, match="Conflicting historical"):
        load_private(store, conflicting, database)
    with duckdb.connect(str(database)) as db:
        assert db.execute("SELECT count(*) FROM privacy_demo.batches").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM privacy_demo.order_batches").fetchone() == (20,)
    payload = manifest.removesuffix("metadata.json") + "customers.json"
    original = store.files[payload]
    store.files[payload] += b" "
    with pytest.raises(ValueError, match="checksum"):
        load_private(store, manifest, database)
    # Even correctly checksummed personal fields are rejected at the loader boundary.
    rows = json.loads(original)
    rows[0]["email"] = "DO-NOT-LEAK@example.invalid"
    store.files[payload] = json.dumps(rows).encode()
    metadata = json.loads(store.files[manifest])
    metadata["files"]["customers.json"] = {
        "bytes": len(store.files[payload]),
        "sha256": hashlib.sha256(store.files[payload]).hexdigest(),
    }
    store.files[manifest] = json.dumps(metadata).encode()
    with pytest.raises(ValueError, match="privacy contract") as failure:
        load_private(store, manifest, database)
    assert "DO-NOT-LEAK" not in str(failure.value)


def test_loader_obeys_common_warehouse_lock(client, key, tmp_path, monkeypatch):
    store = MemoryStore()
    manifest = land_private(store, retrieve(client, key, after_id=0, limit=2), "a" * 32)
    database = tmp_path / "retailflow.duckdb"
    duckdb.connect(str(database)).close()

    def immediate_lock(path, **kwargs):
        assert kwargs["mode"] == 0o660
        return FileLock(path, mode=kwargs["mode"], timeout=0)

    monkeypatch.setattr(customer_ingestion, "FileLock", immediate_lock)
    with FileLock(str(database) + ".lock", mode=0o660):
        with pytest.raises(Timeout):
            load_private(store, manifest, database)
    with duckdb.connect(str(database)) as db:
        assert db.execute("SELECT count(*) FROM information_schema.tables").fetchone() == (0,)


def test_key_environment_priority_and_invalid_key_is_redacted(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "RETAILFLOW_CUSTOMER_PRIVACY_KEY=" + base64.b64encode(bytes(range(32))).decode()
    )
    monkeypatch.delenv("RETAILFLOW_CUSTOMER_PRIVACY_KEY", raising=False)
    assert PrivacyKey.from_environment().value == bytes(range(32))
    monkeypatch.setenv("RETAILFLOW_CUSTOMER_PRIVACY_KEY", "DO-NOT-LEAK")
    with pytest.raises(ValueError) as failure:
        PrivacyKey.from_environment()
    assert "DO-NOT-LEAK" not in str(failure.value)
