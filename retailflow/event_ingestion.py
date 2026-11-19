"""Immutable event landing and idempotent, locked warehouse writes for both new paths."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import duckdb
from filelock import FileLock

from retailflow.event_contracts import InventoryMovement, SourceEvent, decode, encode


class Store(Protocol):
    def put(self, name: str, payload: bytes, content_type: str) -> None: ...
    def read(self, name: str) -> bytes: ...


SCHEMA = """
CREATE SCHEMA IF NOT EXISTS streaming;
CREATE SCHEMA IF NOT EXISTS event_driven;
CREATE SCHEMA IF NOT EXISTS parallel_ops;
CREATE TABLE IF NOT EXISTS streaming.inventory_movements (
 event_id VARCHAR PRIMARY KEY, dataset_version VARCHAR, seed BIGINT,
 interval_seconds INTEGER, sequence BIGINT, occurred_at TIMESTAMPTZ,
 product_id VARCHAR, warehouse_id VARCHAR, quantity_delta INTEGER, reason VARCHAR);
CREATE TABLE IF NOT EXISTS event_driven.shipment_updates (
 event_id VARCHAR PRIMARY KEY, dataset_version VARCHAR, seed BIGINT,
 interval_seconds INTEGER, sequence BIGINT, occurred_at TIMESTAMPTZ,
 order_id BIGINT, order_interval_seconds INTEGER, order_dataset_version VARCHAR,
 shipment_id VARCHAR, status VARCHAR);
CREATE TABLE IF NOT EXISTS parallel_ops.events (
 event_id VARCHAR PRIMARY KEY, sha256 VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS parallel_ops.batches (
 manifest VARCHAR PRIMARY KEY, sha256 VARCHAR NOT NULL, event_id VARCHAR NOT NULL,
 dataset_version VARCHAR NOT NULL, seed BIGINT, interval_seconds INTEGER,
 occurred_at TIMESTAMPTZ, loaded_at TIMESTAMPTZ, transport VARCHAR,
 topic VARCHAR, partition_id INTEGER, offset_id BIGINT);
"""


def land_event(
    store: Store,
    event: SourceEvent,
    *,
    topic: str | None = None,
    partition: int | None = None,
    offset: int | None = None,
) -> str:
    event = decode(encode(event))
    if isinstance(event, InventoryMovement):
        if (
            topic != "retailflow.inventory.v1"
            or type(partition) is not int
            or type(offset) is not int
        ):
            raise ValueError("Inventory requires its Kafka topic/partition/offset lineage")
        if not 0 <= partition < 3 or offset < 0:
            raise ValueError("Invalid Kafka position")
        prefix = f"stream-inventory/{topic}/partition={partition}/offset={offset}"
        transport = "kafka"
    else:
        if any(value is not None for value in (topic, partition, offset)):
            raise ValueError("Shipments must arrive through the webhook path")
        prefix = "webhook-shipments/event=" + event.event_id
        transport = "webhook"
    payload = encode(event)
    blob = prefix + "/data.json"
    metadata = {
        "manifest_version": 1,
        "event_id": event.event_id,
        "payload_blob": blob,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "transport": transport,
        "topic": topic,
        "partition": partition,
        "offset": offset,
    }
    store.put(blob, payload, "application/json")
    if store.read(blob) != payload:
        raise ValueError("Event upload verification failed")
    manifest = prefix + "/metadata.json"
    encoded = json.dumps(metadata, sort_keys=True).encode()
    store.put(manifest, encoded, "application/json")
    if store.read(manifest) != encoded:
        raise ValueError("Event manifest verification failed")
    return manifest


def load_event(store: Store, manifest: str, database: Path, *, lock_timeout: float = 3) -> dict:
    if not re.fullmatch(
        r"(?:webhook-shipments/event=[a-f0-9]{64}|"
        r"stream-inventory/retailflow[.]inventory[.]v1/partition=[0-2]/offset=[0-9]+)/metadata[.]json",
        manifest,
    ):
        raise ValueError("Unapproved event manifest")
    encoded = store.read(manifest)
    metadata = json.loads(encoded)
    if set(metadata) != {
        "manifest_version",
        "event_id",
        "payload_blob",
        "sha256",
        "bytes",
        "transport",
        "topic",
        "partition",
        "offset",
    }:
        raise ValueError("Event manifest contract changed")
    blob = manifest.removesuffix("metadata.json") + "data.json"
    if metadata["manifest_version"] != 1 or metadata["payload_blob"] != blob:
        raise ValueError("Event manifest points outside its batch")
    payload = store.read(blob)
    digest = hashlib.sha256(payload).hexdigest()
    if metadata["sha256"] != digest or metadata["bytes"] != len(payload):
        raise ValueError("Event checksum mismatch")
    event = decode(payload)
    if encode(event) != payload or metadata["event_id"] != event.event_id:
        raise ValueError("Event payload is not canonical")
    # Reconstruct all lineage metadata and paths to reject forged transport values.
    if isinstance(event, InventoryMovement):
        if metadata["transport"] != "kafka" or metadata["topic"] != "retailflow.inventory.v1":
            raise ValueError("Inventory transport changed")
        if type(metadata["partition"]) is not int or type(metadata["offset"]) is not int:
            raise ValueError("Kafka position changed")
        expected = (
            f"stream-inventory/{metadata['topic']}/partition={metadata['partition']}"
            f"/offset={metadata['offset']}/metadata.json"
        )
    else:
        if metadata["transport"] != "webhook" or any(
            metadata[name] is not None for name in ("topic", "partition", "offset")
        ):
            raise ValueError("Shipment transport changed")
        expected = "webhook-shipments/event=" + event.event_id + "/metadata.json"
    if expected != manifest:
        raise ValueError("Event manifest identity changed")
    if not database.is_file():
        raise FileNotFoundError("Refusing to create an unreviewed warehouse")
    manifest_hash = hashlib.sha256(encoded).hexdigest()
    with FileLock(str(database) + ".lock", mode=0o660, timeout=lock_timeout):
        with duckdb.connect(str(database), config={"threads": 1, "memory_limit": "256MB"}) as db:
            db.execute("BEGIN TRANSACTION")
            try:
                db.execute(SCHEMA)
                previous = db.execute(
                    "SELECT sha256 FROM parallel_ops.batches WHERE manifest=?", [manifest]
                ).fetchone()
                if previous:
                    if previous[0] != manifest_hash:
                        raise ValueError("Historical event manifest changed")
                    db.execute("COMMIT")
                    return {"already_loaded": True, "inserted": False}
                canonical = db.execute(
                    "SELECT sha256 FROM parallel_ops.events WHERE event_id=?", [event.event_id]
                ).fetchone()
                if canonical and canonical[0] != digest:
                    raise ValueError("Conflicting historical event")
                if not canonical:
                    common = [
                        event.event_id,
                        event.dataset_version,
                        event.seed,
                        event.interval_seconds,
                        event.sequence,
                        event.occurred_at,
                    ]
                    if isinstance(event, InventoryMovement):
                        db.execute(
                            "INSERT INTO streaming.inventory_movements "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            [
                                *common,
                                event.product_id,
                                event.warehouse_id,
                                event.quantity_delta,
                                event.reason,
                            ],
                        )
                    else:
                        db.execute(
                            "INSERT INTO event_driven.shipment_updates "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            [
                                *common,
                                event.order_id,
                                event.order_interval_seconds,
                                event.order_dataset_version,
                                event.shipment_id,
                                event.status,
                            ],
                        )
                    db.execute(
                        "INSERT INTO parallel_ops.events VALUES (?,?)", [event.event_id, digest]
                    )
                db.execute(
                    "INSERT INTO parallel_ops.batches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        manifest,
                        manifest_hash,
                        event.event_id,
                        event.dataset_version,
                        event.seed,
                        event.interval_seconds,
                        event.occurred_at,
                        datetime.now(UTC),
                        metadata["transport"],
                        metadata["topic"],
                        metadata["partition"],
                        metadata["offset"],
                    ],
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
    return {"already_loaded": False, "inserted": canonical is None}


def ingest(store: Store, event: SourceEvent, database: Path, **lineage) -> dict:
    manifest = land_event(store, event, **lineage)
    return load_event(store, manifest, database)
