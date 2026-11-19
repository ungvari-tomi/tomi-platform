"""Versioned synthetic inventory and shipment events, independent of batch orders."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from retailflow.api import EPOCH


def identity(kind: str, seed: int, interval: int, sequence: int, order_interval: int = 0) -> str:
    return hashlib.sha256(
        f"{kind}:{seed}:{interval}:{sequence}:{order_interval}".encode()
    ).hexdigest()


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    seed: int = Field(ge=-(2**31), lt=2**31)
    interval_seconds: int = Field(ge=5, le=3600)
    sequence: int = Field(ge=1, le=10**10)
    occurred_at: datetime

    @model_validator(mode="after")
    def validate_identity(self):
        if self.occurred_at.utcoffset() is None or self.occurred_at != EPOCH + timedelta(
            seconds=(self.sequence - 1) * self.interval_seconds
        ):
            raise ValueError("Event timestamp does not match its source slot")
        expected = identity(
            self.dataset_version,
            self.seed,
            self.interval_seconds,
            self.sequence,
            getattr(self, "order_interval_seconds", 0),
        )
        if expected != self.event_id:
            raise ValueError("Event identity does not match its source profile")
        return self


class InventoryMovement(Event):
    dataset_version: Literal["inventory-v1"] = "inventory-v1"
    product_id: str = Field(pattern=r"^product-(00[1-9]|01[0-9]|020)$")
    warehouse_id: Literal["WH-EU-01", "WH-EU-02", "WH-EU-03"]
    quantity_delta: int = Field(ge=-50, le=50)
    reason: Literal["sale", "restock", "return"]

    @model_validator(mode="after")
    def validate_movement(self):
        if not self.quantity_delta or (self.reason == "sale") != (self.quantity_delta < 0):
            raise ValueError("Movement sign does not match its reason")
        return self


class ShipmentUpdate(Event):
    dataset_version: Literal["shipments-v1"] = "shipments-v1"
    order_id: int = Field(ge=1)
    order_interval_seconds: int = Field(ge=1, le=3600)
    order_dataset_version: Literal["orders-v3"] = "orders-v3"
    shipment_id: str = Field(pattern=r"^shipment-[a-f0-9]{24}$")
    status: Literal["dispatched", "in_transit", "delivered"]

    @model_validator(mode="after")
    def validate_order(self):
        start_slot = ((self.sequence - 1) // 3) * 3
        expected_order = start_slot * self.interval_seconds // self.order_interval_seconds + 1
        shipment = hashlib.sha256(
            f"shipment:{self.seed}:{self.order_interval_seconds}:{expected_order}".encode()
        ).hexdigest()[:24]
        if self.order_id != expected_order or self.shipment_id != "shipment-" + shipment:
            raise ValueError("Shipment must reference its generated order")
        if self.status != ("dispatched", "in_transit", "delivered")[(self.sequence - 1) % 3]:
            raise ValueError("Shipment status does not match its source sequence")
        return self


SourceEvent = InventoryMovement | ShipmentUpdate
EVENT = TypeAdapter(Annotated[SourceEvent, Field(discriminator="dataset_version")])


def encode(event: SourceEvent) -> bytes:
    return json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()


def decode(payload: bytes, *, now: datetime | None = None) -> SourceEvent:
    if len(payload) > 16384:
        raise ValueError("Event exceeds the payload limit")
    try:
        event = EVENT.validate_json(payload)
        if event.occurred_at > (now or datetime.now(UTC)) + timedelta(seconds=5):
            raise ValueError()
        return event
    except (ValueError, OverflowError):
        raise ValueError("Event violates the synthetic source contract") from None


def generate(
    kind: str, sequence: int, *, seed: int = 42, interval: int = 600, order_interval: int = 600
) -> SourceEvent:
    clock = EPOCH + timedelta(seconds=(sequence - 1) * interval)
    common = {
        "seed": seed,
        "interval_seconds": interval,
        "sequence": sequence,
        "occurred_at": clock,
    }
    if kind == "inventory-v1":
        value = int.from_bytes(hashlib.sha256(f"inventory:{seed}:{sequence}".encode()).digest())
        reason = ("sale", "restock", "return")[value % 3]
        return InventoryMovement(
            **common,
            event_id=identity(kind, seed, interval, sequence),
            product_id=f"product-{1 + value % 20:03d}",
            warehouse_id=f"WH-EU-{1 + value % 3:02d}",
            quantity_delta=(1 + value % 50) * (-1 if reason == "sale" else 1),
            reason=reason,
        )
    if kind != "shipments-v1":
        raise ValueError("Unsupported synthetic event source")
    order_id = ((sequence - 1) // 3) * 3 * interval // order_interval + 1
    shipment = hashlib.sha256(f"shipment:{seed}:{order_interval}:{order_id}".encode()).hexdigest()[
        :24
    ]
    return ShipmentUpdate(
        **common,
        event_id=identity(kind, seed, interval, sequence, order_interval),
        order_id=order_id,
        order_interval_seconds=order_interval,
        shipment_id="shipment-" + shipment,
        status=("dispatched", "in_transit", "delivered")[(sequence - 1) % 3],
    )
