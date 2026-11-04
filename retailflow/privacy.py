"""Keyed customer pseudonyms and strict minimal persisted records; not anonymisation."""

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

from retailflow.api import CustomerOrderPage, Order
from retailflow.currencies import valid_price
from retailflow.customers import Country, CustomerPage


@dataclass(frozen=True)
class PrivacyKey:
    value: bytes = field(repr=False)
    key_id: str = "local-v1"

    def __post_init__(self) -> None:
        if len(self.value) != 32 or not re.fullmatch(r"[a-z0-9-]{1,24}", self.key_id):
            raise ValueError("A 32-byte privacy key and safe key ID are required")

    @classmethod
    def from_environment(cls) -> "PrivacyKey":
        local = dotenv_values(".env", interpolate=False)
        try:
            encoded = os.environ.get(
                "RETAILFLOW_CUSTOMER_PRIVACY_KEY", local.get("RETAILFLOW_CUSTOMER_PRIVACY_KEY")
            )
            key_id = os.environ.get(
                "RETAILFLOW_CUSTOMER_PRIVACY_KEY_ID",
                local.get("RETAILFLOW_CUSTOMER_PRIVACY_KEY_ID") or "local-v1",
            )
            return cls(base64.b64decode(encoded or "", validate=True), key_id)
        except (TypeError, ValueError):
            raise ValueError(
                "Initialize or configure the ignored local customer privacy key"
            ) from None

    def token(self, identifier: str) -> str:
        message = f"retailflow/customer/v1:{self.key_id}:{identifier}".encode()
        return f"ct1_{self.key_id}_" + hmac.new(self.value, message, hashlib.sha256).hexdigest()


class PrivateOrder(Order):
    model_config = ConfigDict(extra="forbid")
    product_id: str = Field(pattern=r"^product-(00[1-9]|01[0-9]|020)$")
    customer_token: str = Field(pattern=r"^ct1_[a-z0-9-]{1,24}_[a-f0-9]{64}$")


class PrivateCustomer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_token: str = Field(pattern=r"^ct1_[a-z0-9-]{1,24}_[a-f0-9]{64}$")
    country: Country
    age_band: Literal["under-18", "18-24", "25-34", "35-44", "45-54", "55-64", "65+"]
    synthetic: Literal[True] = True


def age_band(birth: date, as_of: date) -> str:
    years = as_of.year - birth.year - ((as_of.month, as_of.day) < (birth.month, birth.day))
    if years < 0:
        raise ValueError("Invalid synthetic birth date")
    for bound, label in [
        (18, "under-18"),
        (25, "18-24"),
        (35, "25-34"),
        (45, "35-44"),
        (55, "45-54"),
        (65, "55-64"),
    ]:
        if years < bound:
            return label
    return "65+"


def minimize(
    orders: CustomerOrderPage, customers: CustomerPage, key: PrivacyKey
) -> tuple[list[dict], list[dict]]:
    references = {row.customer_id for row in orders.orders}
    lookup = {row.customer_id: row for row in customers.customers}
    if (
        len(lookup) != len(customers.customers)
        or set(lookup) != references
        or set(customers.order_ids) != {row.order_id for row in orders.orders}
        or (customers.seed, customers.interval_seconds) != (orders.seed, orders.interval_seconds)
    ):
        raise ValueError("Customer references do not match the supplied orders")
    safe_orders = []
    for row in orders.orders:
        customer = lookup[row.customer_id]
        if customer.country != row.country:
            raise ValueError("Customer country does not match referenced order")
        if row.total_minor != row.quantity * row.unit_price_minor or not valid_price(
            row.country, row.currency, row.unit_price_minor, "orders-v3"
        ):
            raise ValueError("Customer-linked order violates the price contract")
        safe_orders.append(
            PrivateOrder(
                **row.model_dump(exclude={"customer_id"}), customer_token=key.token(row.customer_id)
            ).model_dump(mode="json")
        )
    safe_customers = [
        PrivateCustomer(
            customer_token=key.token(row.customer_id),
            country=row.country,
            age_band=age_band(row.birth_date, orders.observed_at.date()),
        ).model_dump(mode="json")
        for row in sorted(customers.customers, key=lambda item: item.customer_id)
    ]
    return safe_orders, safe_customers


def initialize_key() -> None:
    local = dotenv_values(".env", interpolate=False)
    if os.environ.get("RETAILFLOW_CUSTOMER_PRIVACY_KEY") or local.get(
        "RETAILFLOW_CUSTOMER_PRIVACY_KEY"
    ):
        PrivacyKey.from_environment()
        return
    encoded = base64.b64encode(secrets.token_bytes(32)).decode()
    with open(".env", "a", encoding="utf-8") as output:
        output.write("\nRETAILFLOW_CUSTOMER_PRIVACY_KEY=" + encoded + "\n")
