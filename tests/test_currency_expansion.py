import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import duckdb
import httpx
import pytest

from retailflow import extraction
from retailflow.api import Settings, create_app, generate_order
from retailflow.currencies import COUNTRY_PROFILES, FX_QUOTES
from retailflow.exchange_rates import fetch_eur_rate_response
from retailflow.fx_coverage import backfill, coverage
from retailflow.landing import land_orders
from retailflow.warehouse import DDL, load_manifest


class Store:
    def __init__(self):
        self.blobs = {}
        self.fail = False

    def put(self, name, payload, content_type):
        if name in self.blobs and self.blobs[name] != payload:
            raise ValueError("Attempted overwrite")
        self.blobs[name] = payload
        if self.fail and name.endswith("metadata.json"):
            self.fail = False
            raise OSError("Acknowledgment loss")

    def read(self, name):
        return self.blobs[name]


def test_all_currencies_and_legacy_v2_remain_deterministic():
    for version in ["orders-v2", "orders-v3"]:
        settings = Settings(dataset_version=version)
        orders = [generate_order(i, settings) for i in range(1, 1001)]
        assert orders == [generate_order(i, settings) for i in range(1, 1001)]
        if version == "orders-v2":
            import hashlib

            for order in orders:
                value = int.from_bytes(
                    hashlib.sha256(f"orders-v1:42:{order.order_id}".encode()).digest(), "big"
                )
                assert order.country == ("HU", "DE", "FR")[value % 3]
                assert order.unit_price_minor == (
                    (2000 + (value // 7) % 78001) * 100
                    if order.country == "HU"
                    else 500 + (value // 7) % 19501
                )
        else:
            assert {o.currency for o in orders} == {"EUR", *FX_QUOTES}
            assert {o.country for o in orders} == set(COUNTRY_PROFILES)


def test_both_dataset_versions_land_and_load_without_collision(tmp_path):
    store = Store()
    database = tmp_path / "retailflow.duckdb"

    async def run():
        for version in ["orders-v2", "orders-v3"]:
            app = create_app(
                Settings(dataset_version=version),
                clock=lambda: datetime(2026, 11, 2, 17, tzinfo=UTC),
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                response = await client.get("/v1/orders?limit=100")
            assert response.json()["dataset_version"] == version
            manifest = land_orders(
                store, response, batch_id=version, retrieved_at=datetime.now(UTC)
            )
            assert load_manifest(store, manifest, database)["rows_inserted"] == 100

    asyncio.run(run())
    with duckdb.connect(str(database)) as connection:
        assert connection.execute("select count(*) from raw.orders").fetchone() == (200,)
        assert connection.execute("select count(*) from ops.datasets").fetchone() == (2,)
        assert connection.execute("select count(distinct currency) from raw.orders").fetchone() == (
            10,
        )


@pytest.mark.parametrize("quote", FX_QUOTES)
def test_generic_ecb_response_validates_requested_pair(quote):
    def response(request):
        assert request.url.params["quotes"] == quote
        return httpx.Response(
            200,
            json=[{"date": "2026-10-30", "base": "EUR", "quote": quote, "rate": 4.123456}],
            request=request,
        )

    observation, _ = fetch_eur_rate_response(
        date(2026, 11, 1), quote=quote, transport=httpx.MockTransport(response)
    )
    assert observation.quote == quote and observation.rate == Decimal("4.123456")
    assert observation.rate_date == date(2026, 10, 30)


def test_backfill_preserves_response_after_upload_failure_and_skips_covered_pairs(
    monkeypatch, tmp_path
):
    database = tmp_path / "retailflow.duckdb"
    with duckdb.connect(str(database)) as c:
        c.execute(DDL)
        c.execute(
            "insert into raw.orders values ('d',1,'2026-10-31T12:00:00Z',"
            "'PL','PLN','p',1,2000,2000,'m')"
        )
    calls = []

    def fetch(requested, *, quote):
        calls.append((requested, quote))
        return fetch_eur_rate_response(
            requested,
            quote=quote,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=[{"date": "2026-10-30", "base": "EUR", "quote": quote, "rate": 4.25}],
                    request=request,
                )
            ),
        )

    monkeypatch.setattr("retailflow.extraction.fetch_eur_rate_response", fetch)
    store = Store()
    store.fail = True
    with pytest.raises(OSError):
        backfill(database, store, tmp_path / "state")
    assert backfill(database, store, tmp_path / "state")["attempted_pairs"] == 1
    assert len(calls) == 1
    assert backfill(database, store, tmp_path / "state")["attempted_pairs"] == 0
    assert coverage(database) == [
        {
            "order_date": "2026-10-31",
            "currency": "PLN",
            "rate_date": "2026-10-30",
            "status": "covered",
        }
    ]


def test_backfill_bounds_scope_before_any_network_call(tmp_path):
    database = tmp_path / "retailflow.duckdb"
    with duckdb.connect(str(database)) as c:
        c.execute(DDL)
        c.execute(
            "insert into raw.orders values ('d',1,'2026-10-30T12:00:00Z',"
            "'HU','HUF','p',1,200000,200000,'m')"
        )
        c.execute(
            "insert into raw.orders values ('d',2,'2026-10-31T12:00:00Z',"
            "'HU','HUF','p',1,200000,200000,'m')"
        )
    with pytest.raises(ValueError, match="max_pairs"):
        backfill(database, Store(), tmp_path / "state", max_pairs=1)


def test_scheduled_v3_namespace_preserves_legacy_checkpoint(monkeypatch, tmp_path):
    legacy = tmp_path / "orders-checkpoint.json"
    legacy.write_text('{"identity":{"version":"orders-v2"},"cursor":123}')
    monkeypatch.setenv("RETAILFLOW_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("RETAILFLOW_DATASET_VERSION", "orders-v3")
    store = Store()
    store.ensure_container = lambda: None
    store.close = lambda: None
    monkeypatch.setattr("retailflow.blob_storage.BlobStore", lambda: store)
    seen = []
    monkeypatch.setattr(
        extraction,
        "extract_orders",
        lambda store, client, state, run_id, **kwargs: seen.append((state, kwargs)) or {},
    )
    extraction.scheduled_orders("migration-check")
    assert seen == [(tmp_path / "orders-v3", {"expected_version": "orders-v3"})]
    assert json.loads(legacy.read_text())["cursor"] == 123


def test_pair_staging_is_distinct_and_replay_makes_no_new_requests(monkeypatch, tmp_path):
    calls = []

    def fetch(requested, *, quote):
        calls.append(quote)
        return fetch_eur_rate_response(
            requested,
            quote=quote,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=[{"date": "2026-10-30", "base": "EUR", "quote": quote, "rate": 4.25}],
                    request=request,
                )
            ),
        )

    monkeypatch.setattr(extraction, "fetch_eur_rate_response", fetch)
    monkeypatch.setattr(extraction, "fetch_eur_huf_response", lambda day: fetch(day, quote="HUF"))
    store = Store()
    manifests = [
        extraction.extract_rates(store, tmp_path, "run", date(2026, 10, 31), quote=q)
        for q in FX_QUOTES
    ]
    assert len(set(manifests)) == 9 and len(calls) == 9
    assert [
        extraction.extract_rates(store, tmp_path, "run", date(2026, 10, 31), quote=q)
        for q in FX_QUOTES
    ] == manifests
    assert len(calls) == 9
