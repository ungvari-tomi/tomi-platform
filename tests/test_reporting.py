"""Snapshot safety, financial coverage and publication failure regressions."""

import json
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from filelock import FileLock, Timeout

from infra.dbt import reporting, run


@pytest.fixture
def sales_warehouse(tmp_path):
    database = tmp_path / "retailflow.duckdb"
    with duckdb.connect(str(database)) as db:
        db.execute("CREATE SCHEMA staging; CREATE SCHEMA raw")
        db.execute("CREATE TABLE raw.private_sentinel AS SELECT 'synthetic-direct-detail' AS name")
        db.execute(
            reporting.CREATE_REPORT.replace("TABLE daily_sales", "TABLE staging.daily_sales")
        )
        db.executemany(
            "INSERT INTO staging.daily_sales VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "profile-a",
                    date(2026, 11, 19),
                    "DE",
                    "product-001",
                    "EUR",
                    2,
                    3,
                    2,
                    0,
                    9007199254740993,
                    datetime(2026, 11, 19, 12, tzinfo=UTC),
                ),
                (
                    "profile-a",
                    date(2026, 11, 19),
                    "HU",
                    "product-002",
                    "HUF",
                    1,
                    1,
                    0,
                    1,
                    None,
                    datetime(2026, 11, 19, 12, tzinfo=UTC),
                ),
            ],
        )
    return database


def test_immutable_aggregate_snapshots_preserve_exact_cents_and_old_readers(
    sales_warehouse, tmp_path
):
    root = tmp_path / "reports"
    with FileLock(str(sales_warehouse) + ".lock", mode=0o660) as lock:
        first = reporting.publish_report(sales_warehouse, root, warehouse_lock=lock)
        assert lock.is_locked
    manifest, rows = reporting.load_snapshot(root, reporting.current_version(root))
    assert manifest == first and len(rows) == 2
    assert rows[0]["revenue_eur_minor"] == 9007199254740993
    assert rows[1]["revenue_eur_minor"] is None
    assert not any("name" in row or "customer_id" in row for row in rows)
    with duckdb.connect(str(sales_warehouse)) as db:
        db.execute("UPDATE staging.daily_sales SET units_sold=4 WHERE country_code='DE'")
    second = reporting.publish_report(sales_warehouse, root)
    assert second["version"] != first["version"]
    assert reporting.load_snapshot(root, first["version"])[1][0]["units_sold"] == 3
    assert reporting.load_snapshot(root, second["version"])[1][0]["units_sold"] == 4
    with duckdb.connect(str(sales_warehouse), read_only=True) as db:
        assert db.execute("SELECT * FROM raw.private_sentinel").fetchone() == (
            "synthetic-direct-detail",
        )


@pytest.mark.parametrize("corruption", ["coverage", "null_revenue", "extra_field", "row_limit"])
def test_failed_publication_preserves_previous_pointer(
    sales_warehouse, tmp_path, monkeypatch, corruption
):
    root = tmp_path / "reports"
    previous = reporting.publish_report(sales_warehouse, root)
    with duckdb.connect(str(sales_warehouse)) as db:
        if corruption == "coverage":
            db.execute("UPDATE staging.daily_sales SET fx_uncovered_orders=99")
        elif corruption == "null_revenue":
            db.execute(
                "UPDATE staging.daily_sales SET revenue_eur_minor=NULL WHERE fx_covered_orders>0"
            )
        elif corruption == "extra_field":
            db.execute("ALTER TABLE staging.daily_sales ADD COLUMN email VARCHAR")
        else:
            monkeypatch.setattr(reporting, "MAX_ROWS", 1)
    with pytest.raises(ValueError):
        reporting.publish_report(sales_warehouse, root)
    assert reporting.current_version(root) == previous["version"]


def test_snapshot_checksum_and_pointer_traversal_fail_closed(sales_warehouse, tmp_path):
    root = tmp_path / "reports"
    report = reporting.publish_report(sales_warehouse, root)
    database = root / report["version"] / "retailflow.duckdb"
    with database.open("ab") as output:
        output.write(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        reporting.load_snapshot(root, report["version"])
    (root / "current.json").write_text(json.dumps({"format_version": 1, "version": "../warehouse"}))
    with pytest.raises(ValueError, match="version"):
        reporting.current_version(root)
    with pytest.raises(ValueError, match="version"):
        reporting.load_snapshot(root, "../warehouse")


def test_pointer_publication_io_failure_keeps_previous_report(
    sales_warehouse, tmp_path, monkeypatch
):
    root = tmp_path / "reports"
    previous = reporting.publish_report(sales_warehouse, root)
    replace = reporting.os.replace

    def failed_pointer(source, destination):
        if destination.name == "current.json":
            raise OSError("simulated pointer publication failure")
        return replace(source, destination)

    monkeypatch.setattr(reporting.os, "replace", failed_pointer)
    with pytest.raises(OSError, match="publication failure"):
        reporting.publish_report(sales_warehouse, root)
    assert reporting.current_version(root) == previous["version"]
    assert reporting.load_snapshot(root, previous["version"])[0] == previous


def test_reporting_wrapper_publishes_only_after_all_phases_with_same_lock(tmp_path, monkeypatch):
    database = tmp_path / "retailflow.duckdb"
    database.write_bytes(b"fixture")
    monkeypatch.setenv("RETAILFLOW_REPORTING_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(run.shutil, "which", lambda _: "dbt")
    calls = []

    def cli(command, **kwargs):
        with pytest.raises(Timeout):
            FileLock(str(database) + ".lock").acquire(timeout=0)
        calls.append(command[1])
        return SimpleNamespace(returncode=0)

    def publisher(source, root, *, warehouse_lock):
        assert source == database and warehouse_lock.is_locked
        assert calls == ["source", "test", "build"]
        return {"version": "fixture"}

    monkeypatch.setattr(run.subprocess, "run", cli)
    monkeypatch.setattr(run.runpy, "run_path", lambda _: {"publish_report": publisher})
    assert run.execute("reporting-pipeline", database, tmp_path / "artifacts") == 0
    calls.clear()
    monkeypatch.setattr(run.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    monkeypatch.setattr(
        run.runpy, "run_path", lambda _: pytest.fail("Failed build attempted publication")
    )
    assert run.execute("reporting-pipeline", database, tmp_path / "artifacts") == 1


def test_dashboard_lock_preserves_shared_engine_and_hash_export():
    root = Path(__file__).resolve().parents[1]
    shared = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))["package"]
    dashboard = tomllib.loads((root / "infra/dashboard/uv.lock").read_text(encoding="utf-8"))[
        "package"
    ]
    for name in ["duckdb", "filelock", "pytz"]:
        assert next(p["version"] for p in dashboard if p["name"] == name) == next(
            p["version"] for p in shared if p["name"] == name
        )
    package = None
    for line in (root / "infra/dashboard/requirements.txt").read_text().splitlines():
        if not line.startswith("#") and "==" in line:
            name, version = line.split()[0].split("==")
            package = next(p for p in dashboard if p["name"] == name)
            assert version == package["version"]
        if "--hash=" in line:
            digest = line.split("--hash=", 1)[1].strip().rstrip("\\").strip()
            assert package and digest in {
                item["hash"]
                for item in [*package.get("wheels", []), package.get("sdist", {})]
                if "hash" in item
            }
