"""Strict public tool and evidence contracts. No SQL or filesystem arguments."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Source = Literal["sales", "catalog"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SalesScope(StrictModel):
    source: Source = "sales"
    dataset_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    start_date: date
    end_date: date
    countries: list[str] = Field(default_factory=list, max_length=11)
    products: list[str] = Field(default_factory=list, max_length=20)
    categories: list[Literal["electronics", "apparel", "homeware", "Unmatched"]] = Field(
        default_factory=list, max_length=4
    )
    limit: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def period(self):
        if self.end_date < self.start_date or (self.end_date - self.start_date).days > 365:
            raise ValueError("Choose an ordered date range of at most 366 days")
        if self.categories and self.source != "catalog":
            raise ValueError("Category analysis requires the catalog snapshot")
        return self


class InventoryScope(StrictModel):
    seed: int = Field(ge=-(2**63), le=2**63 - 1)
    interval_seconds: int = Field(ge=1, le=3600)
    products: list[str] = Field(default_factory=list, max_length=20)
    categories: list[Literal["electronics", "apparel", "homeware", "Unmatched"]] = Field(
        default_factory=list, max_length=4
    )
    warehouses: list[str] = Field(default_factory=list, max_length=100)
    limit: int = Field(default=10, ge=1, le=20)


class Lookup(StrictModel):
    product_id: str = Field(pattern=r"^product-(00[1-9]|01[0-9]|020)$")


class NoArguments(StrictModel):
    pass


class Evidence(StrictModel):
    source: Source
    version: str = Field(pattern=r"^[a-f0-9]{32}$")
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    published_at: str
    catalog_retrieved_at: str | None = None
    latest_order_at: str | None = None
    latest_inventory_at: str | None = None


class ToolResult(StrictModel):
    tool: str
    status: Literal["ok", "insufficient_data"] = "ok"
    scope: dict
    facts: dict
    evidence: list[Evidence]
    warnings: list[str]
    limitations: list[str]


class AnalysisContext(StrictModel):
    source: Source = "catalog"
    dataset_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    start_date: date | None = None
    end_date: date | None = None
    inventory_seed: int | None = None
    inventory_interval_seconds: int | None = None
