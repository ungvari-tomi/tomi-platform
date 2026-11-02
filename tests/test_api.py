import asyncio
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from retailflow.api import EPOCH, OrderPage, Settings, create_app, generate_order
from retailflow.check_freshness import validate_freshness
from retailflow.currencies import COUNTRY_PROFILES, FX_QUOTES, valid_price


def test_orders_advance_only_when_time_advances() -> None:
    async def scenario() -> None:
        now = EPOCH + timedelta(seconds=102)
        app = create_app(Settings(interval_seconds=5), clock=lambda: now)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.get("/v1/orders", params={"limit": 3})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            first = OrderPage.model_validate(response.json())
            assert first.dataset_version == "orders-v3"
            assert [row.order_id for row in first.orders] == [19, 20, 21]
            validate_freshness(first, now)
            repeat = await client.get("/v1/orders", params={"limit": 3})
            assert repeat.json() == response.json()
            empty = await client.get("/v1/orders", params={"after_id": 21})
            assert empty.json()["orders"] == []
            now += timedelta(seconds=10)
            later = await client.get("/v1/orders", params={"after_id": 21})
            page = OrderPage.model_validate(later.json())
            assert [row.order_id for row in page.orders] == [22, 23]
            assert page.latest_available_at > first.latest_available_at
            assert page.data_age_seconds == 2
            validate_freshness(page, now)
            with pytest.raises(ValueError, match="stale"):
                validate_freshness(first, now + timedelta(seconds=20))

    asyncio.run(scenario())


def test_pagination_and_input_validation() -> None:
    async def scenario() -> None:
        app = create_app(Settings(interval_seconds=5), clock=lambda: EPOCH + timedelta(seconds=20))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            first = (await client.get("/v1/orders?after_id=0&limit=2")).json()
            assert [row["order_id"] for row in first["orders"]] == [1, 2]
            assert first["has_more"]
            second = (await client.get("/v1/orders?after_id=2&limit=100")).json()
            assert [row["order_id"] for row in second["orders"]] == [3, 4, 5]
            assert not second["has_more"]
            for query in ("limit=0", "limit=101", "after_id=-1", "after_id=bad"):
                assert (await client.get(f"/v1/orders?{query}")).status_code == 422
            assert (await client.get("/health")).json()["status"] == "ok"

    asyncio.run(scenario())


def test_deterministic_orders_and_business_rules() -> None:
    settings = Settings(seed=7)
    records = [generate_order(index, settings) for index in range(1, 101)]
    assert records == [generate_order(index, settings) for index in range(1, 101)]
    assert records != [generate_order(index, Settings(seed=8)) for index in range(1, 101)]
    for order in records:
        assert order.total_minor == order.quantity * order.unit_price_minor
        assert order.currency == COUNTRY_PROFILES[order.country][0]
    assert len({order.order_id for order in records}) == 100


def test_currency_specific_price_ranges() -> None:
    currencies = set()
    for order_id in range(1, 1001):
        order = generate_order(order_id, Settings())
        currencies.add(order.currency)
        assert valid_price(order.country, order.currency, order.unit_price_minor, "orders-v3")
        assert order.total_minor == order.unit_price_minor * order.quantity
    assert currencies == {"EUR", *FX_QUOTES}


@pytest.mark.parametrize("interval", [0, -1, 3601])
def test_invalid_interval_rejected(interval: int) -> None:
    with pytest.raises(ValidationError):
        Settings(interval_seconds=interval)


def test_default_interval_is_ten_minutes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RETAILFLOW_INTERVAL_SECONDS", raising=False)
    assert Settings().interval_seconds == 600
    assert Settings.from_environment().interval_seconds == 600
    monkeypatch.setenv("RETAILFLOW_INTERVAL_SECONDS", "5")
    assert Settings.from_environment().interval_seconds == 5


def test_ten_minute_generation_boundary() -> None:
    async def scenario() -> None:
        now = EPOCH + timedelta(seconds=601)
        app = create_app(Settings(), clock=lambda: now)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            first = (await client.get("/v1/orders")).json()
            now += timedelta(seconds=5)
            second = (await client.get("/v1/orders")).json()
            assert first["orders"] == second["orders"]
            now = EPOCH + timedelta(seconds=1200)
            third = (await client.get("/v1/orders?after_id=2")).json()
            assert [row["order_id"] for row in third["orders"]] == [3]
            assert third["data_age_seconds"] == 0

    asyncio.run(scenario())


def test_dotenv_loading_and_environment_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RETAILFLOW_SEED", raising=False)
    monkeypatch.delenv("RETAILFLOW_INTERVAL_SECONDS", raising=False)
    local = tmp_path / ".env"
    local.write_text("RETAILFLOW_SEED=7\nRETAILFLOW_INTERVAL_SECONDS=5\n", encoding="utf-8")
    assert Settings.from_environment() == Settings(seed=7, interval_seconds=5)
    # File loading must not leak settings into other app instances or processes.
    import os

    assert "RETAILFLOW_SEED" not in os.environ
    monkeypatch.setenv("RETAILFLOW_INTERVAL_SECONDS", "600")
    assert Settings.from_environment() == Settings(seed=7, interval_seconds=600)
    monkeypatch.setenv("RETAILFLOW_SEED", "9")
    assert Settings.from_environment() == Settings(seed=9, interval_seconds=600)


@pytest.mark.parametrize("value", ["0", "invalid", ""])
def test_invalid_dotenv_configuration_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RETAILFLOW_INTERVAL_SECONDS", raising=False)
    (tmp_path / ".env").write_text(f"RETAILFLOW_INTERVAL_SECONDS={value}\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        Settings.from_environment()
