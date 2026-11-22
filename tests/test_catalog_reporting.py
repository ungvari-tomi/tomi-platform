"""Catalog snapshot isolation, exact money, bounded schemas and safe publication."""

import json
from datetime import UTC, datetime

import duckdb
import pytest
from filelock import FileLock, Timeout
from test_reporting import sales_warehouse as sales_warehouse

from infra.dbt import catalog_reporting as catalog
from infra.dbt import reporting


@pytest.fixture
def warehouse(sales_warehouse):
    with duckdb.connect(str(sales_warehouse)) as db:
        db.execute("CREATE SCHEMA ops")
        db.execute("CREATE TABLE ops.loaded_batches (source VARCHAR,retrieved_at TIMESTAMPTZ)")
        db.execute(
            "INSERT INTO ops.loaded_batches VALUES "
            "('product-catalog/products-v1',CURRENT_TIMESTAMP)"
        )
        fields = ",".join(f"{name} {kind}" for name, kind in catalog.TABLES["products"])
        db.execute("CREATE TABLE staging.current_products (" + fields + ")")
        for number in range(1, 21):
            db.execute(
                "INSERT INTO staging.current_products VALUES (?,?,?,?,?,?,?,?)",
                [
                    f"product-{number:03d}",
                    f"RF-{number:04d}",
                    f"Synthetic product {number}",
                    "electronics",
                    "active",
                    1,
                    datetime(2025, 1, 1, tzinfo=UTC),
                    datetime.now(UTC),
                ],
            )
        db.execute("""CREATE TABLE staging.catalog_inventory AS SELECT 42::BIGINT AS seed,
            600::INTEGER AS interval_seconds,'product-001' AS product_id,
            'warehouse-001' AS warehouse_id,
            'Synthetic product 1' AS current_product_name,'electronics' AS current_product_category,
            'active' AS catalog_status,3::INTEGER AS quantity_delta,
            CURRENT_TIMESTAMP AS occurred_at""")
        db.execute("""INSERT INTO staging.catalog_inventory SELECT seed,interval_seconds,product_id,
            warehouse_id,current_product_name,current_product_category,catalog_status,-1,occurred_at
            FROM staging.catalog_inventory""")
    return sales_warehouse


def test_catalog_roundtrip_preserves_sales_pointer_money_and_private_boundary(warehouse, tmp_path):
    root = tmp_path / "reports"
    old = reporting.publish_report(warehouse, root)
    original = (root / "current.json").read_bytes()
    manifest = catalog.publish(warehouse, root / "catalog")
    loaded, rows = catalog.load_snapshot(root / "catalog", manifest["version"])
    assert loaded == manifest
    assert rows["catalog_sales"][0]["revenue_eur_minor"] == 9007199254740993
    assert rows["catalog_sales"][1]["revenue_eur_minor"] is None
    movement = rows["inventory_movements"][0]
    assert (
        movement["net_quantity_moved"],
        movement["inbound_units"],
        movement["outbound_units"],
    ) == (2, 3, 1)
    assert len(rows["products"]) == 20
    assert (root / "current.json").read_bytes() == original
    assert reporting.load_snapshot(root, old["version"])[1]
    assert not any(
        "customer" in field or "document_json" in field
        for table in rows.values()
        for row in table
        for field in row
    )


def test_failed_catalog_publish_preserves_pointer_and_previous_immutable_version(
    warehouse, tmp_path
):
    root = tmp_path / "reports"
    first = catalog.publish(warehouse, root)
    pointer = (root / "current.json").read_bytes()
    with duckdb.connect(str(warehouse)) as db:
        db.execute("UPDATE staging.current_products SET category='invalid'")
    with pytest.raises(ValueError, match="contract"):
        catalog.publish(warehouse, root)
    assert (root / "current.json").read_bytes() == pointer
    assert catalog.load_snapshot(root, first["version"])[0] == first


@pytest.mark.parametrize(
    "corrupt", ["bytes", "extra_table", "extra_column", "manifest_rows", "naive_time"]
)
def test_catalog_reader_rejects_corruption(warehouse, tmp_path, corrupt):
    root = tmp_path / "reports"
    manifest = catalog.publish(warehouse, root)
    directory = root / manifest["version"]
    database = directory / "retailflow.duckdb"
    if corrupt == "bytes":
        with database.open("ab") as handle:
            handle.write(b"corrupt")
    elif corrupt in {"extra_table", "extra_column"}:
        with duckdb.connect(str(database)) as db:
            db.execute(
                "CREATE TABLE private_details (name VARCHAR)"
                if corrupt == "extra_table"
                else "ALTER TABLE products ADD COLUMN email VARCHAR"
            )
        manifest.update(bytes=database.stat().st_size, sha256=reporting.checksum(database))
    elif corrupt == "manifest_rows":
        manifest["rows"]["catalog_sales"] = True
    else:
        manifest["catalog_retrieved_at"] = "2026-11-22T12:00:00"
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        catalog.load_snapshot(root, manifest["version"])


def test_catalog_publish_obeys_common_lock_and_checks_database_boundary(warehouse, tmp_path):
    with FileLock(str(warehouse) + ".lock", mode=0o660):
        other = FileLock(str(warehouse) + ".lock", mode=0o660, timeout=0)
        with pytest.raises(Timeout):
            catalog.publish(warehouse, tmp_path / "reports", warehouse_lock=other)
    with pytest.raises(ValueError, match="separate"):
        catalog.publish(warehouse, warehouse.parent)


def test_sales_validation_table_identifiers_are_closed_not_user_sql(warehouse):
    with duckdb.connect(str(warehouse)) as db:
        with pytest.raises(ValueError, match="Unsupported aggregate"):
            reporting.validate_report(db, "catalog_sales; DROP SCHEMA raw CASCADE")
        assert db.execute("SELECT COUNT(*) FROM raw.private_sentinel").fetchone() == (1,)
