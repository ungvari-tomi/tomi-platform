"""Freeze separately verified immutable reports per analysis turn; no warehouse access."""

import re
from pathlib import Path

from infra.dbt import catalog_reporting, reporting
from retailflow_ai.contracts import Evidence, Source

COUNTRIES = {"HU", "DE", "FR", "PL", "CZ", "RO", "GB", "CH", "SE", "NO", "DK"}
CATEGORIES = {"electronics", "apparel", "homeware"}


class DataUnavailable(ValueError):
    """Public code only; original exceptions/paths must never reach a model."""


class Reports:
    def __init__(self, root: Path):
        self.root = root
        self._reports: dict = {}

    def load(self, source: Source) -> tuple[dict, dict]:
        if source not in {"sales", "catalog"}:
            raise DataUnavailable("unsupported_source")
        if source not in self._reports:
            try:
                root = self.root if source == "sales" else self.root / "catalog"
                version = reporting.current_version(root)
                if source == "sales":
                    manifest, rows = reporting.load_snapshot(root, version)
                    tables = {"catalog_sales": rows}
                else:
                    manifest, tables = catalog_reporting.load_snapshot(root, version)
                # Verified reporting schemas alone do not constrain label vocabularies.
                # Only bounded identifiers/enums enter model-visible tool results.
                for row in tables["catalog_sales"]:
                    for field in ("dataset_id", "product_id"):
                        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", row[field]):
                            raise ValueError("Unsafe identifier")
                    if row["country_code"] not in COUNTRIES:
                        raise ValueError("Unsupported country")
                    if source == "catalog" and row["category"] not in CATEGORIES | {None}:
                        raise ValueError("Unsupported category")
                for row in tables.get("inventory_movements", []):
                    for field in ("product_id", "warehouse_id"):
                        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", row[field]):
                            raise ValueError("Unsafe inventory identifier")
                for row in tables.get("products", []):
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", row["sku"]):
                        raise ValueError("Unsafe SKU")
                self._reports[source] = (manifest, tables)
            except Exception:
                raise DataUnavailable("snapshot_unavailable_or_invalid") from None
        return self._reports[source]

    def evidence(self, source: Source) -> Evidence:
        manifest, _ = self.load(source)
        return Evidence(
            source=source,
            version=manifest["version"],
            payload_sha256=manifest["sha256"],
            published_at=manifest["published_at"],
            latest_order_at=manifest.get("latest_order_at"),
            catalog_retrieved_at=manifest.get("catalog_retrieved_at"),
            latest_inventory_at=manifest.get("latest_inventory_at"),
        )

    def metadata(self) -> dict:
        available = {}
        for source in ("sales", "catalog"):
            try:
                manifest, tables = self.load(source)
            except DataUnavailable:
                available[source] = {"available": False}
                continue
            rows = tables["catalog_sales"]
            profiles = []
            for dataset in sorted({r["dataset_id"] for r in rows}):
                dates = [r["order_date"] for r in rows if r["dataset_id"] == dataset]
                profiles.append(
                    {
                        "dataset_id": dataset,
                        "first_date": min(dates).isoformat(),
                        "last_date": max(dates).isoformat(),
                    }
                )
            inventory = sorted(
                {(r["seed"], r["interval_seconds"]) for r in tables.get("inventory_movements", [])}
            )
            available[source] = {
                "available": True,
                "version": manifest["version"],
                "sales_profiles": profiles,
                "inventory_profiles": [{"seed": s, "interval_seconds": i} for s, i in inventory],
                "evidence": self.evidence(source).model_dump(),
            }
        return available
