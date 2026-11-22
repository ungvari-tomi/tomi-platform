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
    rank_by: Literal["orders", "units", "covered_revenue_eur_minor"] = "orders"

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


class SalesMetrics(StrictModel):
    orders: int = Field(ge=0)
    units: int = Field(ge=0)
    fx_covered_orders: int = Field(ge=0)
    fx_uncovered_orders: int = Field(ge=0)
    covered_revenue_eur_minor: int | None
    fx_coverage_bps: int | None = Field(ge=0, le=10000)
    aggregate_rows: int = Field(ge=0)


class GroupMetrics(SalesMetrics):
    key: str = Field(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class PerformanceFacts(StrictModel):
    totals: SalesMetrics
    ranked_by: Literal["orders", "units", "covered_revenue_eur_minor"]
    groups: list[GroupMetrics] = Field(max_length=20)


class ComparisonFacts(StrictModel):
    current: SalesMetrics
    previous: SalesMetrics
    previous_start_date: str
    previous_end_date: str
    revenue_delta_eur_minor: int | None
    revenue_change_bps: int | None
    order_change_bps: int | None


class CoverageFacts(SalesMetrics):
    unavailability_reason: str = Field(max_length=100)


class MovementRank(StrictModel):
    product_id: str = Field(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    warehouse_id: str = Field(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    movement_count: int = Field(ge=0)
    net_quantity_moved: int


class InventoryFacts(StrictModel):
    movement_count: int = Field(ge=0)
    net_quantity_moved: int
    inbound_units: int = Field(ge=0)
    outbound_units: int = Field(ge=0)
    largest_absolute_net_movements: list[MovementRank] = Field(max_length=20)


class ProductFacts(Lookup):
    sku: str = Field(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    category: Literal["electronics", "apparel", "homeware"]
    catalog_status: Literal["active", "retired"]
    revision: int = Field(ge=1)
    updated_at: str
    catalog_observed_at: str


class ToolResult(StrictModel):
    tool: Literal[
        "snapshot_metadata",
        "sales_summary",
        "country_performance",
        "product_performance",
        "category_performance",
        "period_comparison",
        "fx_coverage",
        "inventory_movements",
        "catalog_lookup",
    ]
    status: Literal["ok", "insufficient_data"] = "ok"
    scope: dict
    facts: dict
    evidence: list[Evidence]
    warnings: list[str]
    limitations: list[str]

    @model_validator(mode="after")
    def validated_facts(self):
        schemas = {
            "sales_summary": SalesMetrics,
            "country_performance": PerformanceFacts,
            "product_performance": PerformanceFacts,
            "category_performance": PerformanceFacts,
            "period_comparison": ComparisonFacts,
            "fx_coverage": CoverageFacts,
            "inventory_movements": InventoryFacts,
            "catalog_lookup": ProductFacts,
        }
        if self.tool in schemas:
            schemas[self.tool].model_validate(self.facts)
        return self


class AnalysisContext(StrictModel):
    source: Source = "catalog"
    dataset_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    start_date: date | None = None
    end_date: date | None = None
    inventory_seed: int | None = Field(default=None, ge=-(2**63), le=2**63 - 1)
    inventory_interval_seconds: int | None = Field(default=None, ge=1, le=3600)

    @model_validator(mode="after")
    def valid_period(self):
        if self.start_date is not None and self.end_date is not None:
            if self.end_date < self.start_date or (self.end_date - self.start_date).days > 365:
                raise ValueError("Choose an ordered period of at most 366 days")
        return self
