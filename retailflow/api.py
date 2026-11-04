"""Stateless synthetic orders, materialized on demand from UTC time slots."""

import hashlib
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from retailflow.currencies import COUNTRY_PROFILES, DATASET_VERSION
from retailflow.customers import CustomerPage, customer_id_for, generate_customer

EPOCH = datetime(2025, 1, 1, tzinfo=UTC)


class Settings(BaseModel):
    seed: int = 42
    interval_seconds: int = Field(default=600, ge=1, le=3600)
    dataset_version: Literal["orders-v2", "orders-v3"] = DATASET_VERSION

    @classmethod
    def from_environment(cls) -> Self:
        # Read only the working directory's file, without mutating os.environ or
        # expanding references to other environment variables.
        local = dotenv_values(".env", encoding="utf-8", interpolate=False)
        return cls(
            seed=os.environ.get("RETAILFLOW_SEED", local.get("RETAILFLOW_SEED", "42")),
            interval_seconds=os.environ.get(
                "RETAILFLOW_INTERVAL_SECONDS", local.get("RETAILFLOW_INTERVAL_SECONDS", "600")
            ),
            dataset_version=os.environ.get(
                "RETAILFLOW_DATASET_VERSION",
                local.get("RETAILFLOW_DATASET_VERSION", DATASET_VERSION),
            ),
        )


class Order(BaseModel):
    order_id: int = Field(ge=1)
    created_at: datetime
    country: Literal["HU", "DE", "FR", "PL", "CZ", "RO", "GB", "CH", "SE", "NO", "DK"]
    currency: Literal["HUF", "EUR", "PLN", "CZK", "RON", "GBP", "CHF", "SEK", "NOK", "DKK"]
    product_id: str
    quantity: int = Field(ge=1, le=5)
    unit_price_minor: int = Field(gt=0)
    total_minor: int = Field(gt=0)


class OrderPage(BaseModel):
    dataset_version: str = DATASET_VERSION
    seed: int
    interval_seconds: int
    observed_at: datetime
    latest_available_id: int
    latest_available_at: datetime
    data_age_seconds: float
    orders: list[Order]
    next_after_id: int
    has_more: bool


class CustomerOrder(Order):
    model_config = ConfigDict(extra="forbid")
    customer_id: str = Field(pattern=r"^customer--?[0-9]+-[A-Z]{2}-[0-9]{4}$", max_length=100)
    product_id: str = Field(pattern=r"^product-(00[1-9]|01[0-9]|020)$")


class CustomerOrderPage(OrderPage):
    model_config = ConfigDict(extra="forbid")
    dataset_version: Literal["orders-v4"] = "orders-v4"
    orders: list[CustomerOrder]


def generate_order(order_id: int, settings: Settings) -> Order:
    """Stable content across processes; no global random state or real personal data."""
    # Preserve v1 country/product assignments; v2 changes the pricing rules only.
    value = int.from_bytes(
        hashlib.sha256(f"orders-v1:{settings.seed}:{order_id}".encode()).digest(), "big"
    )
    countries = (
        ("HU", "DE", "FR") if settings.dataset_version == "orders-v2" else tuple(COUNTRY_PROFILES)
    )
    country = countries[value % len(countries)]
    quantity = 1 + value % 5
    # Price ranges are illustrative retail assumptions, not market/FX data.
    currency, minimum, maximum, step = COUNTRY_PROFILES[country]
    price = minimum + ((value // 7) % ((maximum - minimum) // step + 1)) * step
    return Order(
        order_id=order_id,
        created_at=EPOCH + timedelta(seconds=(order_id - 1) * settings.interval_seconds),
        country=country,
        currency=currency,
        product_id=f"product-{1 + value % 20:03d}",
        quantity=quantity,
        unit_price_minor=price,
        total_minor=quantity * price,
    )


def create_app(
    settings: Settings | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    settings = settings or Settings.from_environment()
    clock = clock or (lambda: datetime.now(UTC))
    service = FastAPI(title="RetailFlow Synthetic Source", version="0.1.0")

    @service.get("/health")
    def health(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "no-store"
        return {"status": "ok", "service": "retailflow-synthetic-source"}

    @service.get("/v1/orders", response_model=OrderPage)
    def orders(
        response: Response,
        after_id: Annotated[int | None, Query(ge=0)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> OrderPage:
        now = clock().astimezone(UTC)
        latest_id = int((now - EPOCH).total_seconds()) // settings.interval_seconds + 1
        if latest_id < 1:
            raise HTTPException(status_code=503, detail="Clock precedes dataset epoch")
        latest_at = EPOCH + timedelta(seconds=(latest_id - 1) * settings.interval_seconds)
        start = after_id + 1 if after_id is not None else max(1, latest_id - limit + 1)
        end = min(latest_id + 1, start + limit)
        records = [generate_order(index, settings) for index in range(start, end)]
        cursor = records[-1].order_id if records else (after_id or 0)
        response.headers["Cache-Control"] = "no-store"
        return OrderPage(
            dataset_version=settings.dataset_version,
            seed=settings.seed,
            interval_seconds=settings.interval_seconds,
            observed_at=now,
            latest_available_id=latest_id,
            latest_available_at=latest_at,
            data_age_seconds=(now - latest_at).total_seconds(),
            orders=records,
            next_after_id=cursor,
            has_more=cursor < latest_id,
        )

    @service.get("/v2/orders", response_model=CustomerOrderPage)
    def customer_orders(
        response: Response,
        after_id: Annotated[int | None, Query(ge=0)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> CustomerOrderPage:
        # The customer-enabled contract is separate; v2/v3 source history stays reproducible.
        base = orders(response, after_id, limit)
        profile = Settings(
            seed=settings.seed,
            interval_seconds=settings.interval_seconds,
            dataset_version="orders-v3",
        )
        linked = []
        for row in base.orders:
            original = generate_order(row.order_id, profile)
            linked.append(
                CustomerOrder(
                    **original.model_dump(),
                    customer_id=customer_id_for(original.order_id, settings.seed, original.country),
                )
            )
        return CustomerOrderPage(
            **base.model_dump(exclude={"orders", "dataset_version"}), orders=linked
        )

    @service.get("/v2/customers", response_model=CustomerPage)
    def customers(
        response: Response,
        order_ids: Annotated[list[int], Query(min_length=1, max_length=100)],
    ) -> CustomerPage:
        now = clock().astimezone(UTC)
        latest = int((now - EPOCH).total_seconds()) // settings.interval_seconds + 1
        if any(index < 1 or index > latest for index in order_ids):
            raise HTTPException(
                status_code=422, detail="Order reference is outside the source dataset"
            )
        profile = Settings(
            seed=settings.seed,
            interval_seconds=settings.interval_seconds,
            dataset_version="orders-v3",
        )
        referenced = {}
        for index in sorted(set(order_ids)):
            order = generate_order(index, profile)
            identifier = customer_id_for(index, settings.seed, order.country)
            referenced[identifier] = generate_customer(identifier, settings.seed, order.country)
        response.headers["Cache-Control"] = "no-store"
        return CustomerPage(
            seed=settings.seed,
            interval_seconds=settings.interval_seconds,
            order_ids=sorted(set(order_ids)),
            customers=[referenced[key] for key in sorted(referenced)],
        )

    return service


app = create_app()
