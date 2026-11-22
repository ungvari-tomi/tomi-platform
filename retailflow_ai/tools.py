"""Deterministic business calculations over verified, bounded report rows."""

from collections import defaultdict
from datetime import timedelta

from retailflow_ai.contracts import InventoryScope, Lookup, NoArguments, SalesScope, ToolResult
from retailflow_ai.snapshots import COUNTRIES, DataUnavailable, Reports

LIMITATIONS = [
    "Synthetic retail data; results describe observed report rows, not population completeness.",
    "Snapshot publication time is not an independent source-freshness attestation.",
    "Uncovered FX orders stay in counts; incomplete coverage means partial EUR revenue.",
]


def basis_points(numerator: int, denominator: int) -> int | None:
    if denominator <= 0:
        return None
    sign = -1 if numerator < 0 else 1
    return sign * ((abs(numerator) * 10000 * 2 + denominator) // (2 * denominator))


def aggregate(rows: list[dict]) -> dict:
    count = sum(r["order_count"] for r in rows)
    covered = sum(r["fx_covered_orders"] for r in rows)
    return {
        "orders": count,
        "units": sum(r["units_sold"] for r in rows),
        "fx_covered_orders": covered,
        "fx_uncovered_orders": sum(r["fx_uncovered_orders"] for r in rows),
        "covered_revenue_eur_minor": sum(r["revenue_eur_minor"] or 0 for r in rows)
        if covered
        else None,
        "fx_coverage_bps": basis_points(covered, count),
        "aggregate_rows": len(rows),
    }


class Tools:
    SCHEMAS = {
        "snapshot_metadata": (
            NoArguments,
            "List available verified snapshot versions, profiles and date bounds.",
        ),
        "sales_summary": (
            SalesScope,
            "Summarize observed orders/units and covered EUR cents for one profile and period.",
        ),
        "country_performance": (
            SalesScope,
            "Rank countries by the chosen metric; exact covered EUR cents included.",
        ),
        "product_performance": (
            SalesScope,
            "Rank product IDs by the chosen metric without expanding variants.",
        ),
        "category_performance": (
            SalesScope,
            "Rank latest-observed product categories using catalog sales only.",
        ),
        "period_comparison": (
            SalesScope,
            "Compare equal-length adjacent periods within one snapshot and profile.",
        ),
        "fx_coverage": (
            SalesScope,
            "Quantify coverage; these aggregates cannot separate missing from stale FX.",
        ),
        "inventory_movements": (
            InventoryScope,
            "Summarize lifetime movements for one profile; not stock on hand or dated anomalies.",
        ),
        "catalog_lookup": (
            Lookup,
            "Look up approved metadata, including retirement; excludes raw MongoDB documents.",
        ),
    }

    def __init__(self, reports: Reports):
        self.reports = reports

    @classmethod
    def schemas(cls) -> list[dict]:
        return [
            {"name": name, "description": description, "parameters": model.model_json_schema()}
            for name, (model, description) in cls.SCHEMAS.items()
        ]

    def sales_rows(self, params: SalesScope) -> list[dict]:
        _, tables = self.reports.load(params.source)
        rows = tables["catalog_sales"]
        if params.dataset_id not in {r["dataset_id"] for r in rows}:
            raise DataUnavailable("unknown_sales_profile")
        if not set(params.countries) <= COUNTRIES:
            raise DataUnavailable("unknown_country")
        if not set(params.products) <= {r["product_id"] for r in rows}:
            raise DataUnavailable("unknown_product")
        return [
            r
            for r in rows
            if r["dataset_id"] == params.dataset_id
            and params.start_date <= r["order_date"] <= params.end_date
            and (not params.countries or r["country_code"] in params.countries)
            and (not params.products or r["product_id"] in params.products)
            and (not params.categories or (r.get("category") or "Unmatched") in params.categories)
        ]

    def call(self, name: str, arguments: dict) -> ToolResult:
        if name not in self.SCHEMAS:
            raise DataUnavailable("unapproved_tool")
        model = self.SCHEMAS[name][0]
        # JSON-mode strict validation permits ISO dates but rejects bool-as-int and extra fields.
        import json

        params = model.model_validate_json(json.dumps(arguments))
        if name == "snapshot_metadata":
            meta = self.reports.metadata()
            return ToolResult(
                tool=name,
                scope={},
                facts=meta,
                evidence=[
                    self.reports.evidence(s) for s in ("sales", "catalog") if meta[s]["available"]
                ],
                warnings=[],
                limitations=LIMITATIONS,
            )
        if name == "catalog_lookup":
            _, tables = self.reports.load("catalog")
            product = next(
                (r for r in tables["products"] if r["product_id"] == params.product_id), None
            )
            if product is None:
                raise DataUnavailable("unknown_product")
            # Free-form product names are not sent to the model: they are untrusted labels.
            facts = {
                k: (v.isoformat() if hasattr(v, "isoformat") else v)
                for k, v in product.items()
                if k != "product_name"
            }
            return ToolResult(
                tool=name,
                scope=params.model_dump(),
                facts=facts,
                evidence=[self.reports.evidence("catalog")],
                warnings=[],
                limitations=[
                    "Latest observed metadata, including retirement; not order-time validity."
                ],
            )
        if name == "inventory_movements":
            return self.inventory(params)
        if name == "category_performance" and params.source != "catalog":
            raise DataUnavailable("category_requires_catalog_source")
        rows = self.sales_rows(params)
        metrics = aggregate(rows)
        warnings = []
        if not rows:
            warnings.append("No observed rows; this does not prove zero business activity.")
        if metrics["fx_uncovered_orders"]:
            warnings.append("Partial FX coverage; EUR revenue excludes uncovered orders.")
        facts = metrics
        if name in {"country_performance", "product_performance", "category_performance"}:
            field = {
                "country_performance": "country_code",
                "product_performance": "product_id",
                "category_performance": "category",
            }[name]
            groups = defaultdict(list)
            for row in rows:
                groups[row.get(field) or "Unmatched"].append(row)
            facts = {
                "totals": metrics,
                "ranked_by": params.rank_by,
                "groups": sorted(
                    [{"key": k, **aggregate(v)} for k, v in groups.items()],
                    key=lambda r: (r[params.rank_by] is None, -(r[params.rank_by] or 0), r["key"]),
                )[: params.limit],
            }
        if name == "period_comparison":
            length = (params.end_date - params.start_date).days + 1
            earlier = params.model_copy(
                update={
                    "start_date": params.start_date - timedelta(days=length),
                    "end_date": params.start_date - timedelta(days=1),
                }
            )
            previous = aggregate(self.sales_rows(earlier))
            comparable = bool(
                rows
                and previous["orders"]
                and not metrics["fx_uncovered_orders"]
                and not previous["fx_uncovered_orders"]
            )
            delta = (
                metrics["covered_revenue_eur_minor"] - previous["covered_revenue_eur_minor"]
                if comparable
                else None
            )
            facts = {
                "current": metrics,
                "previous": previous,
                "previous_start_date": earlier.start_date.isoformat(),
                "previous_end_date": earlier.end_date.isoformat(),
                "revenue_delta_eur_minor": delta,
                "revenue_change_bps": basis_points(delta, previous["covered_revenue_eur_minor"])
                if comparable
                else None,
                "order_change_bps": basis_points(
                    metrics["orders"] - previous["orders"], previous["orders"]
                ),
            }
            if not comparable:
                warnings.append("Insufficient complete period coverage for a revenue comparison.")
        if name == "fx_coverage":
            facts = {
                **metrics,
                "unavailability_reason": "Missing, stale or unsupported FX cannot be separated.",
            }
        limitations = list(LIMITATIONS)
        if params.source == "catalog":
            limitations.append(
                "Categories are latest observed metadata, not historical order-time validity."
            )
        return ToolResult(
            tool=name,
            status="ok" if rows else "insufficient_data",
            scope=params.model_dump(mode="json"),
            facts=facts,
            evidence=[self.reports.evidence(params.source)],
            warnings=warnings,
            limitations=limitations,
        )

    def inventory(self, params: InventoryScope) -> ToolResult:
        _, tables = self.reports.load("catalog")
        rows = tables["inventory_movements"]
        if (params.seed, params.interval_seconds) not in {
            (r["seed"], r["interval_seconds"]) for r in rows
        }:
            raise DataUnavailable("unknown_inventory_profile")
        if not set(params.products) <= {r["product_id"] for r in rows}:
            raise DataUnavailable("unknown_product")
        if not set(params.warehouses) <= {r["warehouse_id"] for r in rows}:
            raise DataUnavailable("unknown_warehouse")
        rows = [
            r
            for r in rows
            if (r["seed"], r["interval_seconds"]) == (params.seed, params.interval_seconds)
            and (not params.products or r["product_id"] in params.products)
            and (not params.warehouses or r["warehouse_id"] in params.warehouses)
            and (not params.categories or (r["category"] or "Unmatched") in params.categories)
        ]
        facts = {
            field: sum(r[field] for r in rows)
            for field in ("movement_count", "net_quantity_moved", "inbound_units", "outbound_units")
        }
        facts["largest_absolute_net_movements"] = [
            {
                k: r[k]
                for k in ("product_id", "warehouse_id", "movement_count", "net_quantity_moved")
            }
            for r in sorted(
                rows,
                key=lambda r: (-abs(r["net_quantity_moved"]), r["product_id"], r["warehouse_id"]),
            )[: params.limit]
        ]
        return ToolResult(
            tool="inventory_movements",
            status="ok" if rows else "insufficient_data",
            scope=params.model_dump(),
            facts=facts,
            evidence=[self.reports.evidence("catalog")],
            warnings=[],
            limitations=[
                "Net movement is not stock on hand; no opening balance is supplied.",
                "Lifetime aggregates cannot establish dated spikes, causes or forecasts.",
                "Ranks indicate magnitudes only, not statistically demonstrated anomalies.",
                "Inventory seed/interval profiles are never combined.",
            ],
        )
