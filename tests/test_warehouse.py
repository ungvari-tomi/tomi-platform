import asyncio
import csv
import io
import json
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import httpx
import pytest
from filelock import FileLock, Timeout

from infra.duckdb.runner import check_database
from retailflow.api import Settings, create_app
from retailflow.exchange_rates import fetch_eur_huf_response
from retailflow.landing import land, land_orders
from retailflow.warehouse import load_manifest

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)


class Store:
    def __init__(self) -> None:
        self.blobs = {}

    def put(self, name: str, payload: bytes, content_type: str) -> None:
        if name in self.blobs and self.blobs[name] != payload:
            raise ValueError("Overwrite")
        self.blobs[name] = payload

    def read(self, name: str) -> bytes:
        return self.blobs[name]


def orders_batch(store: Store, batch_id: str = "first", mutate=None, seed: int = 42) -> str:
    async def response():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(Settings(seed=seed), clock=lambda: NOW)),
            base_url="http://test",
        ) as client:
            return await client.get("/v1/orders?limit=2")

    original = asyncio.run(response())
    body = original.json()
    if mutate:
        mutate(body)
    response = httpx.Response(200, json=body, request=original.request)
    return land_orders(store, response, batch_id=batch_id, retrieved_at=NOW)


def rates_batch(store: Store, batch_id: str, requested: date) -> str:
    payload = (Path(__file__).parent / "fixtures/frankfurter_eur_huf_2026-10-30.json").read_bytes()
    rate, response = fetch_eur_huf_response(
        requested, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    )
    return land(
        store,
        source="exchange-rates",
        payload=response.content,
        extension="json",
        request={"url": str(response.request.url)},
        source_metadata=rate.model_dump(mode="json"),
        retrieved_at=NOW,
        batch_id=batch_id,
    )


def test_order_replay_and_overlapping_batches_preserve_lineage(tmp_path: Path) -> None:
    store, database = Store(), tmp_path / "warehouse.duckdb"
    first = orders_batch(store)
    assert load_manifest(store, first, database)["rows_inserted"] == 2
    assert load_manifest(store, first, database)["already_loaded"]
    second = orders_batch(store, "overlap")
    assert load_manifest(store, second, database)["rows_inserted"] == 0
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM raw.orders").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM ops.loaded_batches").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM ops.order_batch_records").fetchone() == (4,)


def test_dataset_settings_create_distinct_order_identity(tmp_path: Path) -> None:
    store, database = Store(), tmp_path / "warehouse.duckdb"
    load_manifest(store, orders_batch(store), database)
    assert (
        load_manifest(store, orders_batch(store, "other-seed", seed=7), database)["rows_inserted"]
        == 2
    )
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ops.datasets").fetchone() == (2,)


def test_fx_weekend_requests_share_reference_row_and_keep_request_dates(tmp_path: Path) -> None:
    store, database = Store(), tmp_path / "warehouse.duckdb"
    first = rates_batch(store, "friday", date(2026, 10, 30))
    second = rates_batch(store, "saturday", date(2026, 10, 31))
    assert load_manifest(store, first, database)["rows_inserted"] == 1
    assert load_manifest(store, second, database)["rows_inserted"] == 0
    assert load_manifest(store, second, database)["already_loaded"]
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM raw.exchange_rates").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(DISTINCT requested_date) FROM ops.fx_batch_records"
        ).fetchone() == (2,)
        assert (
            str(connection.execute("SELECT rate FROM raw.exchange_rates").fetchone()[0])
            == "366.330000000000"
        )


def test_payload_corruption_never_creates_database(tmp_path: Path) -> None:
    store, database = Store(), tmp_path / "warehouse.duckdb"
    manifest = orders_batch(store)
    metadata = json.loads(store.blobs[manifest])
    store.blobs[metadata["payload_blob"]] += b"corruption"
    with pytest.raises(ValueError, match="checksum"):
        load_manifest(store, manifest, database)
    assert not database.exists()


def test_conflicting_order_rolls_back_entire_batch(tmp_path: Path) -> None:
    store, database = Store(), tmp_path / "warehouse.duckdb"
    first = orders_batch(store)
    load_manifest(store, first, database)

    def changed(body):
        body["orders"][0]["unit_price_minor"] += 100
        body["orders"][0]["total_minor"] = (
            body["orders"][0]["quantity"] * body["orders"][0]["unit_price_minor"]
        )

    conflict = orders_batch(store, "conflict", mutate=changed)
    # A valid new record is inserted before encountering the conflicting old record.
    metadata = json.loads(store.blobs[conflict])
    rows = list(csv.DictReader(io.StringIO(store.blobs[metadata["payload_blob"]].decode())))
    rows.insert(0, {**rows[1], "order_id": "1"})
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    import hashlib

    payload = output.getvalue().encode()
    metadata.update(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    metadata["source_metadata"]["row_count"] = 3
    store.blobs[metadata["payload_blob"]] = payload
    store.blobs[conflict] = json.dumps(metadata).encode()
    with pytest.raises(ValueError, match="Conflicting historical"):
        load_manifest(store, conflict, database)
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM raw.orders").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM ops.loaded_batches").fetchone() == (1,)


def test_writer_lock_excludes_other_writers_and_health_probe(tmp_path: Path) -> None:
    store, database = Store(), tmp_path / "warehouse.duckdb"
    manifest = orders_batch(store)
    with FileLock(str(database) + ".lock", mode=0o660):
        with pytest.raises(Timeout):
            load_manifest(store, manifest, database, lock_timeout=0.01)
        assert check_database(database, read_only=True)["status"] == "busy"
        assert not database.exists()


def test_modified_loaded_manifest_is_rejected(tmp_path: Path) -> None:
    store, database = Store(), tmp_path / "warehouse.duckdb"
    manifest = orders_batch(store)
    load_manifest(store, manifest, database)
    metadata = json.loads(store.blobs[manifest])
    metadata["retrieved_at"] = "2026-10-31T13:00:00+00:00"
    store.blobs[manifest] = json.dumps(metadata).encode()
    with pytest.raises(ValueError, match="Previously loaded manifest changed"):
        load_manifest(store, manifest, database)


def test_csv_schema_change_fails_before_database_creation(tmp_path: Path) -> None:
    import hashlib

    store, database = Store(), tmp_path / "warehouse.duckdb"
    manifest = orders_batch(store)
    metadata = json.loads(store.blobs[manifest])
    payload = store.blobs[metadata["payload_blob"]].replace(b"order_id,", b"unexpected_column,")
    metadata.update(sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))
    store.blobs[metadata["payload_blob"]] = payload
    store.blobs[manifest] = json.dumps(metadata).encode()
    with pytest.raises(ValueError, match="header"):
        load_manifest(store, manifest, database)
    assert not database.exists()


def test_revised_rate_is_not_silently_overwritten(tmp_path: Path) -> None:
    import hashlib

    store, database = Store(), tmp_path / "warehouse.duckdb"
    load_manifest(store, rates_batch(store, "initial", date(2026, 10, 31)), database)
    revised = rates_batch(store, "revised", date(2026, 10, 31))
    metadata = json.loads(store.blobs[revised])
    payload = store.blobs[metadata["payload_blob"]].replace(b"366.33", b"366.34")
    metadata.update(sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))
    metadata["source_metadata"]["rate"] = "366.34"
    store.blobs[metadata["payload_blob"]] = payload
    store.blobs[revised] = json.dumps(metadata).encode()
    with pytest.raises(ValueError, match="Conflicting published FX"):
        load_manifest(store, revised, database)
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ops.loaded_batches").fetchone() == (1,)
        assert (
            str(connection.execute("SELECT rate FROM raw.exchange_rates").fetchone()[0])
            == "366.330000000000"
        )
