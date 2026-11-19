"""Real dbt publication and Streamlit UI, using disposable anonymous volumes only."""

import os
import subprocess
from uuid import uuid4

import duckdb
import pytest
from test_customer_pipeline_container import disposable_warehouse

from retailflow.warehouse import DDL

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_DASHBOARD_IMAGE") != "1",
    reason="Opt in to disposable Streamlit/dbt images",
)


def test_sales_pipeline_and_read_only_streamlit_filters(tmp_path):
    database = tmp_path / "retailflow.duckdb"
    with duckdb.connect(str(database)) as db:
        db.execute(DDL)
        db.execute("INSERT INTO ops.datasets VALUES ('current', 'orders-v3', 42, 600)")
        db.execute("""INSERT INTO raw.orders VALUES
            ('current',1,CURRENT_TIMESTAMP,'DE','EUR','product-001',1,500,500,'orders'),
            ('current',2,CURRENT_TIMESTAMP,'HU','HUF','product-002',1,200000,200000,'orders'),
            ('current',3,CURRENT_TIMESTAMP - INTERVAL '1 day',
             'HU','HUF','product-002',1,200000,200000,'orders')""")
        for quote in ["HUF", "PLN", "CZK", "RON", "GBP", "CHF", "SEK", "NOK", "DKK"]:
            db.execute(
                "INSERT INTO raw.exchange_rates VALUES ('ECB','EUR',?,CURRENT_DATE,400,'fx')",
                [quote],
            )
            db.execute(
                "INSERT INTO ops.fx_batch_records VALUES "
                "(?,'ECB','EUR',?,CURRENT_DATE,CURRENT_DATE)",
                [quote, quote],
            )
            db.execute(
                """INSERT INTO ops.loaded_batches VALUES (?, 'sha', 'sha', 'exchange-rates',
                CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,1,1,'{}')""",
                [quote],
            )
    name = "dashboard-fixture-" + uuid4().hex

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
            name,
            "--network",
            "none",
            "--mount",
            "type=volume,target=/reports",
            "retailflow-dashboard:validation",
        )
        empty_ui = """
from streamlit.testing.v1 import AppTest
app=AppTest.from_file('/app/app.py').run(timeout=30)
assert not app.exception
assert 'first successfully published' in app.info[0].value
print('PASS empty dashboard')
"""
        docker(
            "run",
            *controls,
            "--volumes-from",
            name + ":ro",
            "--entrypoint",
            "python",
            "retailflow-dashboard:validation",
            "-c",
            empty_ui,
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
                name,
                "--tmpfs",
                "/artifacts:rw,noexec,nosuid,size=64m,uid=50000,gid=0",
                "-e",
                "RETAILFLOW_REPORTING_DIR=/reports",
                "retailflow-dbt:validation",
                "reporting-pipeline",
            ]
            docker(*build)
            ui = """
import os
from pathlib import Path
from streamlit.testing.v1 import AppTest
from reporting import current_version,load_snapshot
assert os.getuid()==10001 and not Path('/warehouse').exists()
assert not any('PASSWORD' in key or key=='AZURITE_ACCOUNT_KEY' for key in os.environ)
try: Path('/reports/forbidden').write_text('no')
except OSError as error: assert error.errno in (13,30)
else: raise AssertionError('Reporting mount is writable')
manifest,rows=load_snapshot(Path('/reports'),current_version(Path('/reports')))
assert sum(row['order_count'] for row in rows)==3
assert sum(row['revenue_eur_minor'] or 0 for row in rows)==1000
assert sum(row['fx_uncovered_orders'] for row in rows)==1
app=AppTest.from_file('/app/app.py').run(timeout=30)
assert not app.exception,app.exception
assert [m.value for m in app.metric]==['€10.00','3','3','66.7%']
assert len(app.warning)==1
app.sidebar.multiselect[0].set_value(['DE']).run()
assert not app.exception
assert [m.value for m in app.metric]==['€5.00','1','1','100.0%']
assert not app.warning
print('PASS exact sales, missing FX, filters, read-only UI and isolated snapshot')
"""
            result = docker(
                "run",
                *controls,
                "--volumes-from",
                name + ":ro",
                "--entrypoint",
                "python",
                "retailflow-dashboard:validation",
                "-c",
                ui,
            )
            assert "PASS exact sales" in result.stdout
            # A failing dbt build must not replace a working snapshot.
            mutate = """
import duckdb
from pathlib import Path
from filelock import FileLock
from reporting import current_version
Path('/reports/before-failure').write_text(current_version(Path('/reports')))
with FileLock('/warehouse/retailflow.duckdb.lock',mode=0o660):
 with duckdb.connect('/warehouse/retailflow.duckdb') as db:
  db.execute('UPDATE raw.orders SET unit_price_minor=1,total_minor=1 WHERE order_id=1')
"""
            docker(
                "run",
                *controls,
                "--volumes-from",
                warehouse,
                "--volumes-from",
                name,
                "--entrypoint",
                "python",
                "retailflow-dbt:validation",
                "-c",
                mutate,
            )
            assert docker(*build, check=False).returncode != 0
            verify = """
from pathlib import Path
from reporting import current_version,load_snapshot
root=Path('/reports')
assert current_version(root)==(root/'before-failure').read_text()
assert sum(row['order_count'] for row in load_snapshot(root,current_version(root))[1])==3
"""
            docker(
                "run",
                *controls,
                "--volumes-from",
                name + ":ro",
                "--entrypoint",
                "python",
                "retailflow-dashboard:validation",
                "-c",
                verify,
            )
    finally:
        result = docker("rm", "--force", "--volumes", name, check=False)
        assert result.returncode == 0 or "No such container" in result.stderr
