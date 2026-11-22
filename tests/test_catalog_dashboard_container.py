"""Full dbt catalog publication and read-only Streamlit interaction in disposable volumes."""

import os
import subprocess
from uuid import uuid4

import duckdb
import pytest
from test_customer_pipeline_container import disposable_warehouse
from test_landing import MemoryStore
from test_product_catalog import snapshot

from retailflow.event_ingestion import SCHEMA
from retailflow.product_catalog import load_manifest
from retailflow.warehouse import DDL

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_DASHBOARD_IMAGE") != "1",
    reason="Opt-in dashboard validation images",
)


def test_catalog_reporting_and_read_only_streamlit_pages(tmp_path):
    database = tmp_path / "retailflow.duckdb"
    with duckdb.connect(str(database)) as db:
        db.execute(DDL)
        db.execute(SCHEMA)
        for profile, seed in [("current", 42), ("other", 99)]:
            db.execute("INSERT INTO ops.datasets VALUES (?, 'orders-v3', ?, 600)", [profile, seed])
            db.execute(
                """INSERT INTO raw.orders VALUES
                (?,1,CURRENT_TIMESTAMP,'DE','EUR','product-001',1,500,500,'orders')""",
                [profile],
            )
        db.execute("""INSERT INTO raw.orders VALUES
            ('current',2,CURRENT_TIMESTAMP,'HU','HUF','product-002',1,200000,200000,'orders'),
            ('current',3,CURRENT_TIMESTAMP - INTERVAL '1 day',
             'HU','HUF','product-002',1,200000,200000,'orders')""")
        for quote in ["HUF", "PLN", "CZK", "RON", "GBP", "CHF", "SEK", "NOK", "DKK"]:
            db.execute(
                "INSERT INTO raw.exchange_rates VALUES ('ECB','EUR',?,CURRENT_DATE,400,'fx')",
                [quote],
            )
            db.execute(
                "INSERT INTO ops.fx_batch_records "
                "VALUES (?,'ECB','EUR',?,CURRENT_DATE,CURRENT_DATE)",
                [quote, quote],
            )
            db.execute(
                """INSERT INTO ops.loaded_batches VALUES (?, 'sha','sha','exchange-rates',
                CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,1,1,'{}')""",
                [quote],
            )
        for event, product, quantity, reason in [
            ("one", "product-001", 3, "restock"),
            ("two", "product-002", -1, "sale"),
        ]:
            db.execute(
                """INSERT INTO streaming.inventory_movements VALUES
                (?,'inventory-v1',42,600,1,CURRENT_TIMESTAMP,?,'warehouse-001',?,?)""",
                [event, product, quantity, reason],
            )
    store = MemoryStore()
    load_manifest(store, snapshot(store), database)
    owner = "catalog-reports-" + uuid4().hex

    def docker(*args, check=True):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=240)
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    controls = [
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--tmpfs",
        "/tmp:rw,nosuid,size=128m",
    ]
    try:
        docker(
            "create",
            "--name",
            owner,
            "--network",
            "none",
            "--mount",
            "type=volume,target=/reports",
            "retailflow-dashboard:validation",
        )
        with disposable_warehouse(database) as warehouse:
            build = [
                "run",
                *controls,
                "--user",
                "50000:0",
                "--group-add",
                "10001",
                "--volumes-from",
                warehouse,
                "--volumes-from",
                owner,
                "--tmpfs",
                "/artifacts:rw,nosuid,size=64m,uid=50000,gid=0",
                "-e",
                "RETAILFLOW_REPORTING_DIR=/reports",
                "retailflow-dbt:validation",
                "catalog-reporting-pipeline",
            ]
            docker(*build)
            ui = r"""
import os
from pathlib import Path
from streamlit.testing.v1 import AppTest
from reporting import current_version
from catalog_reporting import load_snapshot
assert os.getuid()==10001 and not Path('/warehouse').exists()
assert not any(k=='AZURITE_ACCOUNT_KEY' or 'PASSWORD' in k for k in os.environ)
try:Path('/reports/forbidden').write_text('no')
except OSError as e:assert e.errno in (13,30)
else:raise AssertionError('Reporting mount is writable')
manifest,rows=load_snapshot(Path('/reports/catalog'),current_version(Path('/reports/catalog')))
assert len(rows['products'])==20
assert sum(r['order_count'] for r in rows['catalog_sales'])==4
assert sum(r['movement_count'] for r in rows['inventory_movements'])==2
app=AppTest.from_file('/app/app.py').run(timeout=30)
app.sidebar.radio[0].set_value('Catalog and inventory').run(timeout=30)
assert not app.exception,app.exception
metrics={m.label:m.value for m in app.metric}
assert metrics['Catalog sales - covered EUR revenue']=='EUR 10.00',metrics
assert metrics['Catalog orders']=='3' and metrics['Net units moved']=='2'
assert len(app.warning)==1
assert len(app.tabs)==3
app.sidebar.multiselect[0].set_value(['electronics']).run(timeout=30)
assert not app.exception,app.exception
metrics={m.label:m.value for m in app.metric}
assert metrics['Catalog sales - covered EUR revenue']=='EUR 5.00'
assert metrics['Catalog orders']=='1' and metrics['Net units moved']=='3'
assert not app.warning
print('PASS catalog publication, exact sales, category/profile isolation, '
      'inventory and read-only UI')
"""
            result = docker(
                "run",
                *controls,
                "--volumes-from",
                owner + ":ro",
                "--entrypoint",
                "python",
                "retailflow-dashboard:validation",
                "-c",
                ui,
            )
            assert "PASS catalog publication" in result.stdout
            mutate = r"""
from pathlib import Path
from reporting import current_version
from filelock import FileLock
import duckdb
Path('/reports/previous-catalog').write_text(current_version(Path('/reports/catalog')))
with FileLock('/warehouse/retailflow.duckdb.lock',mode=0o660):
 with duckdb.connect('/warehouse/retailflow.duckdb') as db:
  db.execute("UPDATE streaming.inventory_movements "
             "SET occurred_at=CURRENT_TIMESTAMP - INTERVAL '1 day'")
"""
            docker(
                "run",
                *controls,
                "--volumes-from",
                warehouse,
                "--volumes-from",
                owner,
                "--entrypoint",
                "python",
                "retailflow-dbt:validation",
                "-c",
                mutate,
            )
            assert docker(*build, check=False).returncode != 0
            verify = (
                "from pathlib import Path;from reporting import current_version;"
                "assert current_version(Path('/reports/catalog'))=="
                "Path('/reports/previous-catalog').read_text()"
            )
            docker(
                "run",
                *controls,
                "--volumes-from",
                owner + ":ro",
                "--entrypoint",
                "python",
                "retailflow-dashboard:validation",
                "-c",
                verify,
            )
    finally:
        docker("rm", "--force", "--volumes", owner)
