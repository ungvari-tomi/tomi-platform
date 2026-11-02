import asyncio
import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from retailflow.api import Settings, create_app
from retailflow.blob_storage import initialize
from retailflow.exchange_rates import fetch_eur_huf_response
from retailflow.landing import download_batch, land, land_orders

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)


class MemoryStore:
    """A test double; live SDK/Azurite behavior is tested separately."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, name: str, payload: bytes, content_type: str) -> None:
        if name in self.blobs and self.blobs[name] != payload:
            raise ValueError("Refusing overwrite")
        self.blobs[name] = payload

    def read(self, name: str) -> bytes:
        return self.blobs[name]


def test_orders_csv_roundtrip_and_metadata() -> None:
    async def scenario() -> None:
        store = MemoryStore()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(Settings(), clock=lambda: NOW)),
            base_url="http://test",
        ) as client:
            response = await client.get("/v1/orders?limit=3")
        manifest = land_orders(store, response, batch_id="orders-test", retrieved_at=NOW)
        metadata, payload = download_batch(store, manifest)
        rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8"))))
        assert len(rows) == 3
        assert metadata["source_metadata"]["dataset_version"] == "orders-v3"
        assert metadata["source_metadata"]["row_count"] == 3
        assert metadata["source_metadata"]["next_after_id"] == int(rows[-1]["order_id"])
        assert metadata["request"]["url"] == "http://test/v1/orders?limit=3"
        for csv_row, original in zip(rows, response.json()["orders"], strict=True):
            assert csv_row == {key: str(value) for key, value in original.items()}
        # Same payload and metadata can be replayed without duplicates.
        assert land_orders(store, response, batch_id="orders-test", retrieved_at=NOW) == manifest
        assert len(store.blobs) == 2

    asyncio.run(scenario())


def test_frankfurter_body_is_preserved_byte_for_byte() -> None:
    payload = (Path(__file__).parent / "fixtures/frankfurter_eur_huf_2026-10-30.json").read_bytes()
    observation, response = fetch_eur_huf_response(
        NOW.date(), transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    )
    store = MemoryStore()
    manifest = land(
        store,
        source="exchange-rates",
        payload=response.content,
        extension="json",
        request={"url": str(response.request.url)},
        source_metadata=observation.model_dump(mode="json"),
        retrieved_at=NOW,
        batch_id="fx-test",
    )
    metadata, downloaded = download_batch(store, manifest)
    assert downloaded == payload
    assert metadata["source_metadata"]["rate_date"] == "2026-10-30"
    assert metadata["source_metadata"]["requested_date"] == "2026-10-31"
    assert "providers=ecb" in metadata["request"]["url"]


def test_incomplete_upload_has_no_completion_metadata() -> None:
    class CorruptStore(MemoryStore):
        def read(self, name: str) -> bytes:
            return b"corrupted"

    store = CorruptStore()
    with pytest.raises(ValueError, match="checksum"):
        land(
            store,
            source="orders",
            payload=b"test",
            extension="csv",
            request={},
            source_metadata={},
            retrieved_at=NOW,
            batch_id="failed",
        )
    assert len(store.blobs) == 1
    assert not any(name.endswith("metadata.json") for name in store.blobs)


def test_download_detects_corruption() -> None:
    store = MemoryStore()
    manifest = land(
        store,
        source="orders",
        payload=b"test",
        extension="csv",
        request={},
        source_metadata={},
        retrieved_at=NOW,
        batch_id="corruption-test",
    )
    metadata = json.loads(store.blobs[manifest])
    store.blobs[metadata["payload_blob"]] = b"different"
    with pytest.raises(ValueError, match="checksum"):
        download_batch(store, manifest)


def test_csv_conversion_rejects_unknown_columns() -> None:
    from retailflow.api import generate_order

    row = generate_order(1, Settings()).model_dump(mode="json")
    row["new_source_field"] = "must not be silently lost"
    page = {
        "dataset_version": "orders-v2",
        "seed": 42,
        "interval_seconds": 600,
        "observed_at": NOW.isoformat(),
        "latest_available_id": 1,
        "latest_available_at": NOW.isoformat(),
        "data_age_seconds": 0,
        "orders": [row],
        "next_after_id": 1,
        "has_more": False,
    }
    response = httpx.Response(200, json=page, request=httpx.Request("GET", "http://test/v1/orders"))
    store = MemoryStore()
    with pytest.raises(ValueError, match="schema changed"):
        land_orders(store, response, batch_id="schema-test", retrieved_at=NOW)
    assert not store.blobs


def test_key_initialization_preserves_existing_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AZURITE_ACCOUNT_KEY", raising=False)
    path = tmp_path / ".env"
    path.write_text("RETAILFLOW_INTERVAL_SECONDS=600\n", encoding="utf-8")
    initialize()
    configured = path.read_bytes()
    assert b"RETAILFLOW_INTERVAL_SECONDS=600" in configured
    assert b"AZURITE_ACCOUNT_KEY=" in configured
    initialize()
    assert path.read_bytes() == configured
