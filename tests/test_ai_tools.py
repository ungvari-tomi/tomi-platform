"""Mandatory offline financial and evidence tests over real verified snapshot files."""

import json

import duckdb
import pytest
from pydantic import ValidationError
from test_catalog_reporting import warehouse as warehouse
from test_reporting import sales_warehouse as sales_warehouse

from infra.dbt import catalog_reporting, reporting
from retailflow_ai.snapshots import DataUnavailable, Reports
from retailflow_ai.tools import Tools, basis_points


@pytest.fixture
def tools(warehouse, tmp_path):
    root = tmp_path / "reports"
    reporting.publish_report(warehouse, root)
    catalog_reporting.publish(warehouse, root / "catalog")
    return Tools(Reports(root))


def scope(**changes):
    return {
        "source": "catalog",
        "dataset_id": "profile-a",
        "start_date": "2026-11-19",
        "end_date": "2026-11-19",
        **changes,
    }


def test_exact_money_null_fx_and_evidence(tools):
    result = tools.call("sales_summary", scope())
    assert result.facts["covered_revenue_eur_minor"] == 9007199254740993
    assert result.facts["orders"] == 3 and result.facts["fx_uncovered_orders"] == 1
    assert result.facts["fx_coverage_bps"] == 6667 and result.warnings
    assert result.evidence[0].source == "catalog" and len(result.evidence[0].version) == 32
    uncovered = tools.call("fx_coverage", scope(countries=["HU"]))
    assert uncovered.facts["covered_revenue_eur_minor"] is None
    assert "cannot be separated" in uncovered.facts["unavailability_reason"]


def test_country_product_and_category_ranks_are_deterministic(tools):
    assert tools.call("country_performance", scope()).facts["groups"][0]["key"] == "DE"
    assert tools.call("product_performance", scope()).facts["groups"][0]["key"] == "product-001"
    assert tools.call("category_performance", scope()).facts["groups"][0]["orders"] == 3
    with pytest.raises(DataUnavailable, match="category_requires"):
        tools.call("category_performance", scope(source="sales"))


def test_empty_period_and_incomplete_comparison_never_manufacture_revenue(tools):
    result = tools.call("sales_summary", scope(start_date="2026-11-18", end_date="2026-11-18"))
    assert (
        result.status == "insufficient_data" and result.facts["covered_revenue_eur_minor"] is None
    )
    compare = tools.call("period_comparison", scope())
    assert compare.facts["revenue_delta_eur_minor"] is None
    assert compare.facts["revenue_change_bps"] is None and compare.warnings


@pytest.mark.parametrize(
    "changes",
    [
        {"dataset_id": "another-profile"},
        {"countries": ["XX"]},
        {"products": ["missing"]},
    ],
)
def test_unknown_profiles_and_identifiers_fail_closed(tools, changes):
    with pytest.raises(DataUnavailable):
        tools.call("sales_summary", scope(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"start_date": "2026-11-20"},
        {"end_date": "2028-01-01"},
        {"limit": True},
        {"sql": "SELECT * FROM raw.orders"},
        {"limit": 1000},
        {"categories": ["invented"]},
    ],
)
def test_invalid_arguments_are_strict(tools, changes):
    with pytest.raises(ValidationError):
        tools.call("sales_summary", scope(**changes))


def test_inventory_semantics_and_no_dates_or_profile_mixing(tools):
    result = tools.call("inventory_movements", {"seed": 42, "interval_seconds": 600})
    assert result.facts["net_quantity_moved"] == 2 and result.facts["movement_count"] == 2
    assert result.facts["inbound_units"] == 3 and result.facts["outbound_units"] == 1
    assert any("not stock on hand" in s for s in result.limitations)
    with pytest.raises(DataUnavailable):
        tools.call("inventory_movements", {"seed": 99, "interval_seconds": 600})
    with pytest.raises(ValidationError):
        tools.call(
            "inventory_movements", {"seed": 42, "interval_seconds": 600, "date": "2026-11-19"}
        )


def test_metadata_and_catalog_exclude_free_text_labels_and_customer_data(tools):
    meta = tools.call("snapshot_metadata", {})
    assert meta.facts["sales"]["sales_profiles"][0]["dataset_id"] == "profile-a"
    product = tools.call("catalog_lookup", {"product_id": "product-001"})
    assert product.facts["sku"] == "RF-0001" and "product_name" not in product.facts
    serialized = product.model_dump_json()
    assert "private_sentinel" not in serialized and "document_json" not in serialized


def test_snapshot_is_frozen_per_turn_and_corruption_is_sanitized(tools, warehouse):
    first = tools.call("sales_summary", scope())
    root = tools.reports.root
    with duckdb.connect(str(warehouse)) as db:
        db.execute("UPDATE staging.daily_sales SET units_sold=5")
    catalog_reporting.publish(warehouse, root / "catalog")
    assert tools.call("sales_summary", scope()).facts == first.facts
    assert Tools(Reports(root)).call("sales_summary", scope()).facts["units"] == 10
    pointer = json.loads((root / "catalog/current.json").read_text())
    database = root / "catalog" / pointer["version"] / "retailflow.duckdb"
    with database.open("ab") as file:
        file.write(b"corrupt")
    with pytest.raises(DataUnavailable, match="snapshot_unavailable_or_invalid") as failure:
        Tools(Reports(root)).call("sales_summary", scope())
    assert str(root) not in str(failure.value)


def test_half_up_integer_ratios():
    assert basis_points(1, 3) == 3333 and basis_points(-1, 3) == -3333
    assert basis_points(1, 32) == 313 and basis_points(-1, 32) == -313
    assert basis_points(1, 0) is None


def test_retired_products_are_retained(warehouse, tmp_path):
    root = tmp_path / "reports"
    with duckdb.connect(str(warehouse)) as db:
        db.execute(
            "UPDATE staging.current_products SET catalog_status='retired' "
            "WHERE product_id='product-001'"
        )
        db.execute("UPDATE staging.catalog_inventory SET catalog_status='retired'")
    catalog_reporting.publish(warehouse, root / "catalog")
    result = Tools(Reports(root)).call("catalog_lookup", {"product_id": "product-001"})
    assert result.facts["catalog_status"] == "retired"
