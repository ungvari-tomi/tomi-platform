"""Exercise the real Airflow SDK and isolated dbt against disposable masked data."""

import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest
from fastapi.testclient import TestClient
from test_customer_privacy import MemoryStore

from retailflow.api import Settings, create_app
from retailflow.customer_ingestion import extract_private, load_private
from retailflow.privacy import PrivacyKey


@contextmanager
def disposable_warehouse(database: Path) -> Iterator[str]:
    """Copy only this test fixture into an owned Linux volume; never mount live data."""
    owner = "customer-fixture-" + uuid4().hex

    def docker(*args):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    try:
        # Initialization alone runs as root, with limited capabilities and no network.
        # Workload containers below keep their deployed non-root identities.
        docker(
            "create",
            "--name",
            owner,
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "FOWNER",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "FSETID",
            "--security-opt",
            "no-new-privileges:true",
            "--mount",
            "type=volume,target=/warehouse",
            "--entrypoint",
            "python",
            "retailflow-duckdb:validation",
            "-c",
            "import os; from pathlib import Path; "
            "p=Path('/warehouse/retailflow.duckdb'); "
            "os.chown(p,10001,10001); p.chmod(0o660); "
            "os.chown(p.parent,10001,10001); p.parent.chmod(0o2770)",
        )
        docker("cp", str(database), owner + ":/warehouse/retailflow.duckdb")
        docker("start", "--attach", owner)
        yield owner
    finally:
        result = subprocess.run(
            ["docker", "rm", "--force", "--volumes", owner],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode and "No such container" not in result.stderr:
            raise RuntimeError("Disposable customer fixture cleanup failed")


@pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_AIRFLOW_IMAGE") != "1",
    reason="Opt in to local validation images",
)
def test_default_dbt_parse_excludes_customer_models_and_generated_tests(tmp_path):
    root = Path(__file__).resolve().parents[1]
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,size=128m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
    ]
    for source, target, readonly in [
        (root / "infra/dbt/project", "/app/project", True),
        (root / "infra/dbt/run.py", "/app/run.py", True),
    ]:
        command += [
            "--mount",
            f"type=bind,source={source},target={target}" + (",readonly" if readonly else ""),
        ]
    command += [
        "--mount",
        "type=volume,target=/warehouse",
        "--mount",
        "type=volume,target=/artifacts",
        "--entrypoint",
        "python",
    ]
    script = """
import json, os
from pathlib import Path
import duckdb
from run import execute
assert os.getuid() == 10001
path=Path('/warehouse/retailflow.duckdb')
duckdb.connect(str(path)).close()
assert execute('parse',path,Path('/artifacts')) == 0
manifest=json.loads(next(Path('/artifacts').glob('*/target/manifest.json')).read_text())
models=[r['name'] for r in manifest['nodes'].values() if r['resource_type']=='model']
tests=[r for r in manifest['nodes'].values() if r['resource_type']=='test']
assert set(models)=={'stg_orders','stg_exchange_rates','orders_eur'}
assert len(tests)==37
print('PASS: default build excludes customer resources')
"""
    result = subprocess.run(
        [*command, "retailflow-dbt:validation", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: default build excludes customer resources" in result.stdout


@pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_AIRFLOW_IMAGE") != "1",
    reason="Opt in to the local Airflow validation image; no running services are used",
)
def test_real_customer_pipeline_and_deliberate_corruption(tmp_path):
    root = Path(__file__).resolve().parents[1]
    warehouse = tmp_path / "warehouse"
    warehouse.mkdir()
    database = warehouse / "retailflow.duckdb"
    duckdb.connect(str(database)).close()
    store = MemoryStore()
    with TestClient(create_app(Settings())) as client:
        manifest = extract_private(
            store, client, tmp_path / "state", "fixture", PrivacyKey(bytes(range(32))), limit=20
        )
    load_private(store, manifest, database)
    script = r"""
import json, os, runpy, subprocess
from pathlib import Path
import duckdb
from filelock import FileLock
from retailflow.transformation import scheduled_build

assert os.getuid()==50000 and 10001 in os.getgroups()
warehouse=Path('/warehouse')
assert warehouse.stat().st_gid==10001
assert warehouse.stat().st_mode & 0o7777 == 0o2770
assert (warehouse/'retailflow.duckdb').stat().st_mode & 0o777 == 0o660

namespace=runpy.run_path('/opt/airflow/dags/retailflow_extraction.py')
workflow=namespace['customers']()
assert workflow.is_paused_upon_creation and workflow.max_active_runs == 1
assert workflow.get_task('extract').downstream_task_ids == {'load'}
assert workflow.get_task('load').downstream_task_ids == {'dbt_build'}
from retailflow.alerts import ALLOWED_DAGS
assert 'retailflow_customers' in ALLOWED_DAGS

os.environ['RETAILFLOW_CUSTOMER_PRIVACY_KEY']='DO-NOT-PASS-TO-DBT'
os.environ['RETAILFLOW_CUSTOMER_PRIVACY_KEY_FILE']='/run/secrets/not-mounted'
original=subprocess.run
def checked(command, **kwargs):
    env=kwargs['env']
    assert 'RETAILFLOW_CUSTOMER_PRIVACY_KEY' not in env
    assert 'RETAILFLOW_CUSTOMER_PRIVACY_KEY_FILE' not in env
    return original(command, **kwargs)
subprocess.run=checked
result=scheduled_build('retailflow_customers','healthy',1,customers=True)
assert result['models_succeeded']==3
assert result['freshness_sources_passed']==2 and result['freshness_tests_passed']==1
path='/warehouse/retailflow.duckdb'
with FileLock(path+'.lock',mode=0o660):
    with duckdb.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM staging.customer_orders').fetchone()[0]==20
        assert db.execute('SELECT COUNT(*) FROM staging.customer_orders '
                          'WHERE customer_as_of IS NULL').fetchone()[0]==0

def failure(label, mutation, restoration):
    with FileLock(path+'.lock',mode=0o660):
        with duckdb.connect(path) as db:
            db.execute(mutation)
    try:
        scheduled_build('retailflow_customers',label,1,customers=True)
    except subprocess.CalledProcessError:
        print('PASS expected failure:',label,flush=True)
    else:
        raise AssertionError('Corruption passed: '+label)
    finally:
        with FileLock(path+'.lock',mode=0o660):
            with duckdb.connect(path) as db:
                db.execute(restoration)

failure('missing_customer',
 'CREATE TABLE saved_customers AS SELECT * FROM privacy_demo.customers; '
 'DELETE FROM privacy_demo.customers',
 'INSERT INTO privacy_demo.customers SELECT * FROM saved_customers; DROP TABLE saved_customers')
failure('orphan_customer',
 "INSERT INTO privacy_demo.customers SELECT dataset_id,customer_token,CURRENT_DATE-1,"
 "country,age_band,synthetic FROM privacy_demo.customers LIMIT 1",
 'DELETE FROM privacy_demo.customers WHERE as_of=CURRENT_DATE-1')
failure('unexpected_personal_field',
 'ALTER TABLE privacy_demo.customers ADD COLUMN email VARCHAR',
 'ALTER TABLE privacy_demo.customers DROP COLUMN email')
failure('stale_customer_orders',
 "CREATE TABLE saved_clocks AS SELECT order_id,created_at FROM privacy_demo.orders; "
 "UPDATE privacy_demo.orders SET created_at=CURRENT_TIMESTAMP-INTERVAL '31 minutes'",
 'UPDATE privacy_demo.orders o SET created_at=s.created_at FROM saved_clocks s '
 'WHERE s.order_id=o.order_id; DROP TABLE saved_clocks')
print('PASS customer pipeline, paused DAG, private child environment and 4 corruptions',flush=True)
"""
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "50000:0",
        "--group-add",
        "10001",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--tmpfs",
        "/tmp:rw,nosuid,size=128m",
        "-e",
        "RETAILFLOW_DUCKDB_PATH=/warehouse/retailflow.duckdb",
        "-e",
        # Local artifact path, not a credential.
        # pragma: allowlist nextline secret
        "RETAILFLOW_DBT_ARTIFACT_DIR=/opt/airflow/logs/customer-dbt",
    ]
    mounts = [
        (root / "infra/dbt/project", "/opt/dbt/project", True),
        (root / "infra/dbt/run.py", "/opt/dbt/run.py", True),
        (
            root / "dags/retailflow_extraction.py",
            "/opt/airflow/dags/retailflow_extraction.py",
            True,
        ),
        (root / "retailflow/transformation.py", "/opt/airflow/retailflow/transformation.py", True),
        (root / "retailflow/alerts.py", "/opt/airflow/retailflow/alerts.py", True),
    ]
    for source, target, readonly in mounts:
        command += [
            "--mount",
            f"type=bind,source={source},target={target}" + (",readonly" if readonly else ""),
        ]
    command += ["--entrypoint", "python", "retailflow-airflow:validation", "-c", script]
    with disposable_warehouse(database) as owner:
        # Docker initializes the logs volume from the image's scheduler-owned directory.
        command[2:2] = ["--volumes-from", owner, "--mount", "type=volume,target=/opt/airflow/logs"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "4 corruptions" in result.stdout
