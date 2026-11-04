"""Fictional customer identities derived exclusively from referenced synthetic orders."""

import hashlib
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from retailflow.currencies import COUNTRY_PROFILES

Country = Literal["HU", "DE", "FR", "PL", "CZ", "RO", "GB", "CH", "SE", "NO", "DK"]


class SyntheticCustomer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str = Field(pattern=r"^customer--?[0-9]+-[A-Z]{2}-[0-9]{4}$", max_length=100)
    first_name: str = Field(pattern=r"^Demo[0-9]{4}$")
    last_name: Literal["Customer"]
    email: str = Field(pattern=r"^customer-[0-9]{4}@example[.]invalid$")
    phone: str = Field(pattern=r"^NOT-A-PHONE-[0-9]{4}$")
    address: str = Field(pattern=r"^FICTIONAL-ADDRESS-[A-Z]{2}-[0-9]{4}$")
    birth_date: date
    country: Country
    synthetic: Literal[True] = True


class CustomerPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_version: Literal["customers-v1"] = "customers-v1"
    order_dataset_version: Literal["orders-v4"] = "orders-v4"
    seed: int
    interval_seconds: int = Field(ge=1, le=3600)
    order_ids: list[int]
    customers: list[SyntheticCustomer]


def customer_id_for(order_id: int, seed: int, country: str) -> str:
    if order_id < 1 or country not in COUNTRY_PROFILES:
        raise ValueError("Invalid synthetic order reference")
    digest = hashlib.sha256(f"customer-v1:{seed}:{order_id}".encode()).digest()
    return f"customer-{seed}-{country}-{1 + int.from_bytes(digest, 'big') % 1000:04d}"


def generate_customer(customer_id: str, seed: int, country: str) -> SyntheticCustomer:
    bucket = int(customer_id.rsplit("-", 1)[1])
    expected = f"customer-{seed}-{country}-{bucket:04d}"
    if customer_id != expected or country not in COUNTRY_PROFILES or not 1 <= bucket <= 1000:
        raise ValueError("Invalid synthetic customer reference")
    value = int.from_bytes(hashlib.sha256(customer_id.encode()).digest(), "big")
    return SyntheticCustomer(
        customer_id=customer_id,
        first_name=f"Demo{bucket:04d}",
        last_name="Customer",
        email=f"customer-{bucket:04d}@example.invalid",
        phone=f"NOT-A-PHONE-{bucket:04d}",
        address=f"FICTIONAL-ADDRESS-{country}-{bucket:04d}",
        birth_date=date(1960 + value % 46, 1 + value % 12, 1 + value % 28),
        country=country,
    )
