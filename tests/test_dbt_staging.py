"""Build real staging views and prove dbt rejects deliberately corrupted fixtures."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_DBT_IMAGE") != "1", reason="Opt-in dbt image required"
)
ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def test_staging_preserves_grain_precision_and_detects_invalid_rows(tmp_path: Path):
    name = "retailflow-staging-test-" + uuid4().hex

    def docker(*args):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    def build():
        return subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts/dbt-dev.ps1"),
                "build",
                "-ProjectDirectory",
                str(ROOT / "infra/dbt/project"),
                "-ArtifactDirectory",
                str(tmp_path / "artifacts"),
                "-WarehouseVolume",
                volume,
                "-Image",
                "retailflow-dbt:validation",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )

    try:
        docker(
            "run",
            "--name",
            name,
            "--network",
            "none",
            "--mount",
            "type=volume,destination=/warehouse",
            "--entrypoint",
            "python",
            "retailflow-dbt:validation",
            "-c",
            "import duckdb; c=duckdb.connect('/warehouse/retailflow.duckdb'); "
            'c.execute("CREATE SCHEMA raw; '
            "CREATE TABLE raw.orders AS SELECT 'dataset-a' AS dataset_id, 1::BIGINT AS order_id, "
            "TIMESTAMPTZ '2026-10-30 12:00:00+00' AS created_at, "
            "'HU' AS country, 'HUF' AS currency, 'product-001' AS product_id, "
            "2::INTEGER AS quantity, 200000::BIGINT AS unit_price_minor, "
            "400000::BIGINT AS total_minor, 'orders/batch/metadata.json' AS first_manifest; "
            "INSERT INTO raw.orders SELECT 'dataset-b',order_id,created_at,'DE','EUR',product_id, "
            "1,500,500,first_manifest FROM raw.orders; "
            "CREATE TABLE raw.exchange_rates AS SELECT 'ECB' AS provider, 'EUR' AS base, "
            "'HUF' AS quote, DATE '2026-10-30' AS rate_date, "
            "366.330000000001::DECIMAL(24,12) AS rate, "
            "'fx/batch/metadata.json' AS first_manifest; CREATE SCHEMA ops; "
            "CREATE TABLE ops.fx_batch_records AS SELECT 'fx/metadata.json' AS manifest_path, "
            'provider,base,quote,rate_date,rate_date AS requested_date FROM raw.exchange_rates"); '
            "c.close()",
        )
        mounts = json.loads(docker("inspect", name, "--format", "{{json .Mounts}}"))
        volume = next(m["Name"] for m in mounts if m["Destination"] == "/warehouse")
        valid = build()
        assert valid.returncode == 0, valid.stdout + valid.stderr
        evidence = docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--volumes-from",
            name,
            "--entrypoint",
            "python",
            "retailflow-dbt:validation",
            "-c",
            "import duckdb,json; c=duckdb.connect('/warehouse/retailflow.duckdb'); "
            "assert c.execute('SELECT COUNT(*) FROM staging.stg_orders').fetchone()==(2,); "
            "assert str(c.execute('SELECT quote_units_per_base_unit FROM "
            "staging.stg_exchange_rates').fetchone()[0])=='366.330000000001'; "
            "assert c.execute('SELECT SUM(total_minor) FROM raw.orders').fetchone()==(400500,); "
            'print(json.dumps(c.execute("SELECT table_name,table_type FROM '
            "information_schema.tables WHERE table_schema='staging' ORDER BY 1\").fetchall())); "
            'c.execute("INSERT INTO raw.orders SELECT * FROM raw.orders '
            "WHERE dataset_id='dataset-a'; "
            "UPDATE raw.orders SET product_id=NULL, quantity=-1, currency='BAD', total_minor=1 "
            "WHERE dataset_id='dataset-b'; "
            "INSERT INTO raw.exchange_rates SELECT * FROM raw.exchange_rates; "
            "UPDATE raw.exchange_rates SET rate=-1,base='HUF',quote='EUR'\"); c.close()",
        )
        assert json.loads(evidence) == [
            ["orders_eur", "VIEW"],
            ["stg_exchange_rates", "VIEW"],
            ["stg_orders", "VIEW"],
        ]
        invalid = build()
        assert invalid.returncode != 0
        results = [
            json.loads(p.read_text())
            for p in (tmp_path / "artifacts").glob("*/target/run_results.json")
        ]
        assert len(results) == 2
        assert any(
            all(r["status"] in ("success", "pass") for r in run["results"]) for run in results
        )
        failures = {
            r["unique_id"] for run in results for r in run["results"] if r["status"] == "fail"
        }
        for category in (
            "unique_business_key",
            "not_null",
            "positive",
            "accepted_values",
            "orders_business_rules",
        ):
            assert any(category in failure for failure in failures), failures
    finally:
        subprocess.run(["docker", "rm", "--volumes", name], capture_output=True, check=False)
