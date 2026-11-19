"""All three paths coexist; new transports fail closed and retry without duplicates."""

import json
from datetime import UTC, datetime, timedelta

import duckdb
import pytest
from fastapi.testclient import TestClient
from filelock import FileLock, Timeout
from pydantic import ValidationError
from test_customer_privacy import MemoryStore
from test_parallel_container import cleanup_resources

from retailflow.api import EPOCH, Settings, generate_order
from retailflow.event_contracts import decode, encode, generate
from retailflow.event_ingestion import ingest, land_event, load_event
from retailflow.event_runtime import (
    TOPIC,
    consume_one,
    inventory_delivery,
    shipment_delivery,
    source_step,
)
from retailflow.shipment_webhook import create_app, signature


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "retailflow.duckdb"
    with duckdb.connect(str(path)) as db:
        db.execute("CREATE SCHEMA raw; CREATE TABLE raw.batch_sentinel AS SELECT 42 AS value")
    return path


def test_distinct_deterministic_data_and_referenced_orders():
    for interval in [5, 600]:
        for sequence in range(1, 25):
            inventory = generate("inventory-v1", sequence, interval=interval)
            shipment = generate("shipments-v1", sequence, interval=interval)
            assert inventory.event_id != shipment.event_id
            assert encode(inventory) == encode(
                generate("inventory-v1", sequence, interval=interval)
            )
            order = generate_order(shipment.order_id, Settings())
            assert order.created_at <= shipment.occurred_at
            assert decode(encode(inventory)) == inventory
            assert b"customer" not in encode(shipment) and b"address" not in encode(shipment)
    with pytest.raises(ValidationError):
        generate("inventory-v1", 1).model_validate(
            {**generate("inventory-v1", 1).model_dump(), "email": "not-allowed@example.invalid"}
        )
    with pytest.raises(ValueError, match="contract"):
        decode(b'{"dataset_version":"unknown"}')


def test_both_transports_load_and_leave_batch_data_untouched(database):
    store = MemoryStore()
    inventory = generate("inventory-v1", 1)
    shipment = generate("shipments-v1", 1)
    assert ingest(store, inventory, database, topic=TOPIC, partition=0, offset=0)["inserted"]
    assert ingest(store, shipment, database)["inserted"]
    assert ingest(store, shipment, database)["already_loaded"]
    # A duplicate Kafka delivery at another offset adds lineage, not a second business record.
    assert not ingest(store, inventory, database, topic=TOPIC, partition=1, offset=9)["inserted"]
    with duckdb.connect(str(database)) as db:
        assert db.execute("SELECT * FROM raw.batch_sentinel").fetchone() == (42,)
        assert db.execute("SELECT COUNT(*) FROM streaming.inventory_movements").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM event_driven.shipment_updates").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM parallel_ops.batches").fetchone() == (3,)
        assert db.execute("SELECT COUNT(*) FROM parallel_ops.events").fetchone() == (2,)


def test_conflicts_checksums_and_manifest_paths_fail_without_partial_commit(database):
    store = MemoryStore()
    event = generate("inventory-v1", 1)
    manifest = land_event(store, event, topic=TOPIC, partition=0, offset=0)
    load_event(store, manifest, database)
    changed = event.model_copy(
        update={"product_id": "product-020" if event.product_id != "product-020" else "product-001"}
    )
    conflicting = land_event(store, changed, topic=TOPIC, partition=0, offset=1)
    with pytest.raises(ValueError, match="Conflicting"):
        load_event(store, conflicting, database)
    payload = manifest.removesuffix("metadata.json") + "data.json"
    store.files[payload] += b" "
    with pytest.raises(ValueError, match="checksum"):
        load_event(store, manifest, database)
    with pytest.raises(ValueError, match="Unapproved"):
        load_event(store, "../data.json", database)
    with duckdb.connect(str(database)) as db:
        assert db.execute("SELECT COUNT(*) FROM parallel_ops.batches").fetchone() == (1,)


def test_source_outbox_freezes_on_acknowledgement_loss_and_rejects_profile_change(tmp_path):
    state = tmp_path / "inventory"
    sent = []

    def lost_ack(event):
        sent.append(encode(event))
        raise RuntimeError("lost acknowledgement")

    with pytest.raises(RuntimeError):
        source_step(state, "inventory-v1", lost_ack, now=EPOCH + timedelta(seconds=600))
    record = json.loads((state / "checkpoint.json").read_text())
    assert record["next"] == 2 and record["pending"]
    delivered = []
    source_step(
        state,
        "inventory-v1",
        lambda event: delivered.append(encode(event)),
        now=EPOCH + timedelta(seconds=1200),
    )
    assert sent == delivered
    assert json.loads((state / "checkpoint.json").read_text())["next"] == 3
    with pytest.raises(ValueError, match="profile"):
        source_step(state, "inventory-v1", lambda _: None, now=EPOCH, seed=99)


class Message:
    def __init__(self, payload):
        self.payload = payload

    def value(self):
        return self.payload

    def error(self):
        return None

    def topic(self):
        return TOPIC

    def partition(self):
        return 0

    def offset(self):
        return 0


class Consumer:
    def __init__(self, payload):
        self.message = Message(payload)
        self.commits = 0

    def poll(self, timeout):
        return self.message

    def commit(self, **kwargs):
        assert kwargs == {"message": self.message, "asynchronous": False}
        self.commits += 1
        return []


def test_kafka_offset_advances_only_after_verified_commit(database):
    store = MemoryStore()
    consumer = Consumer(encode(generate("inventory-v1", 1)))
    store.fail_manifest_once = True
    with pytest.raises(OSError):
        consume_one(consumer, store, database)
    assert consumer.commits == 0
    assert consume_one(consumer, store, database)
    assert consume_one(consumer, store, database)
    assert consumer.commits == 2
    with duckdb.connect(str(database)) as db:
        assert db.execute("SELECT COUNT(*) FROM streaming.inventory_movements").fetchone() == (1,)
    consumer.message = Message(b'{"email":"do-not-persist@example.invalid"}')
    with pytest.raises(ValueError):
        consume_one(consumer, store, database)
    assert consumer.commits == 2


def test_producer_requires_successful_delivery_callback():
    class Producer:
        def produce(self, topic, *, key, value, on_delivery):
            assert topic == TOPIC and key and value
            on_delivery(RuntimeError("unacknowledged"), None)

        def flush(self, timeout):
            return 0

    with pytest.raises(RuntimeError, match="acknowledged"):
        inventory_delivery(Producer(), generate("inventory-v1", 1))


def test_signed_webhook_retries_replay_and_rejects_invalid_input(database):
    store = MemoryStore()
    key = bytes(range(32))
    now = datetime.now(UTC).timestamp()
    app = create_app(store_factory=lambda: store, database=database, key=key, clock=lambda: now)
    body = encode(generate("shipments-v1", 1))
    stamp = str(int(now))

    def headers(payload=body, timestamp=stamp):
        return {
            "Content-Type": "application/json",
            "X-RetailFlow-Timestamp": timestamp,
            "X-RetailFlow-Signature": signature(key, timestamp, payload),
        }

    with TestClient(app) as client:
        assert (
            client.post(
                "/v1/shipments/events", content=body, headers={"Content-Type": "application/json"}
            ).status_code
            == 401
        )
        stale = str(int(now) - 301)
        assert (
            client.post(
                "/v1/shipments/events", content=body, headers=headers(timestamp=stale)
            ).status_code
            == 401
        )
        store.fail_manifest_once = True
        assert (
            client.post("/v1/shipments/events", content=body, headers=headers()).status_code == 503
        )
        assert (
            client.post("/v1/shipments/events", content=body, headers=headers()).status_code == 202
        )
        replay = client.post("/v1/shipments/events", content=body, headers=headers())
        assert replay.status_code == 202 and replay.json()["already_loaded"]
        shipment_delivery(client, key, generate("shipments-v1", 1))
        bad = json.loads(body)
        bad["address"] = "DO-NOT-LEAK"
        encoded = json.dumps(bad).encode()
        rejected = client.post("/v1/shipments/events", content=encoded, headers=headers(encoded))
        assert rejected.status_code == 422 and "DO-NOT-LEAK" not in rejected.text
        assert not any(b"DO-NOT-LEAK" in value for value in store.files.values())
        assert (
            client.post("/v1/shipments/events", content=b"x" * 16385, headers=headers()).status_code
            == 413
        )
    with duckdb.connect(str(database)) as db:
        assert db.execute("SELECT COUNT(*) FROM event_driven.shipment_updates").fetchone() == (1,)


def test_warehouse_lock_prevents_both_event_writers(database, monkeypatch):
    from retailflow import event_ingestion

    store = MemoryStore()
    manifest = land_event(store, generate("shipments-v1", 1))
    with FileLock(str(database) + ".lock", mode=0o660):
        with pytest.raises(Timeout):
            event_ingestion.load_event(store, manifest, database, lock_timeout=0)
    with duckdb.connect(str(database)) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='event_driven'"
        ).fetchone() == (0,)


def test_cleanup_attempts_all_disposable_resources_and_reports_backend_failure(monkeypatch):
    from types import SimpleNamespace

    from test_parallel_container import subprocess

    commands = []

    def failed(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=1, stderr="Docker backend unavailable")

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        cleanup_resources("parallel-test-" + "a" * 32)
    assert len(commands) == 3
    with pytest.raises(ValueError, match="restricted"):
        cleanup_resources("retailflow-kafka")
    assert len(commands) == 3
