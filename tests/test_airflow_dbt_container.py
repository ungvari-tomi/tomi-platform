"""Execute the real DAG task body as the scheduler UID against disposable data."""

import os
import subprocess
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_AIRFLOW_IMAGE") != "1",
    reason="Updated Airflow and warehouse validation images required",
)


def test_airflow_task_runs_isolated_dbt_and_propagates_quality_failure():
    owner = "retailflow-airflow-dbt-owner-" + uuid4().hex
    worker = "retailflow-airflow-dbt-worker-" + uuid4().hex

    def docker(*args):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    try:
        docker(
            "run",
            "--name",
            owner,
            "--network",
            "none",
            "--mount",
            "type=volume,destination=/warehouse",
            "retailflow-duckdb:validation",
            "python",
            "-c",
            "import duckdb; from pathlib import Path; from runner import permissions; "
            "p=Path('/warehouse/retailflow.duckdb'); c=duckdb.connect(str(p)); "
            'c.execute("CREATE SCHEMA raw; '
            "CREATE TABLE raw.orders AS SELECT 'dataset' AS dataset_id, "
            "1::BIGINT AS order_id, current_timestamp AS created_at, "
            "'DE' AS country, 'EUR' AS currency, 'product-001' AS product_id, "
            "1::INTEGER AS quantity, 500::BIGINT AS unit_price_minor, 500::BIGINT AS total_minor, "
            "'orders/metadata.json' AS first_manifest; "
            "CREATE TABLE raw.exchange_rates AS SELECT 'ECB' AS provider, 'EUR' AS base, "
            "quote, current_date AS rate_date, "
            "366.33::DECIMAL(24,12) AS rate, quote || '/metadata.json' AS first_manifest "
            "FROM (VALUES ('HUF'),('PLN'),('CZK'),('RON'),('GBP'),"
            "('CHF'),('SEK'),('NOK'),('DKK')) q(quote); "
            "CREATE SCHEMA ops; "
            "CREATE TABLE ops.datasets AS SELECT 'dataset' AS dataset_id, "
            "'orders-v3' AS dataset_version, "
            "42::BIGINT AS seed, 600::BIGINT AS interval_seconds; "
            "CREATE TABLE ops.loaded_batches AS SELECT first_manifest AS manifest_path, "
            "'exchange-rates' AS source, current_timestamp AS loaded_at FROM raw.exchange_rates; "
            "CREATE TABLE ops.fx_batch_records AS SELECT first_manifest AS manifest_path, "
            'provider,base,quote,rate_date,rate_date AS requested_date FROM raw.exchange_rates"); '
            "c.close(); permissions(p)",
        )
        script = """
import json,os,runpy,subprocess
from pathlib import Path
from types import SimpleNamespace
import duckdb
from filelock import FileLock
assert os.getuid()==50000 and 10001 in os.getgroups()
subprocess.run(['/opt/dbt/venv/bin/python','-I','-c',
    "import importlib.util,importlib.metadata,sys; "
    "assert importlib.util.find_spec('airflow') is None; "
    "assert importlib.metadata.version('dbt-core')=='1.11.16'; "
    "assert sys.prefix=='/opt/dbt/venv'"], check=True)
namespace=runpy.run_path('/opt/airflow/dags/retailflow_extraction.py')
dag=namespace['orders']()
task=dag.get_task('dbt_build')
context={'dag':dag,'run_id':'manual__integration','ti':SimpleNamespace(try_number=1)}
task.python_callable.__globals__['get_current_context']=lambda:context
result=task.python_callable({'source':'orders','rows_inserted':1})
assert result['models_succeeded']==3 and result['tests_passed']==37, result
assert Path(result['artifact_directory']).is_relative_to('/opt/airflow/logs/dbt')
with FileLock('/warehouse/retailflow.duckdb.lock',mode=0o660,timeout=60):
    with duckdb.connect('/warehouse/retailflow.duckdb') as c:
        assert c.execute('select count(*) from staging.stg_orders').fetchone()==(1,)
        c.execute('update raw.orders set quantity=-1')
context['ti'].try_number=2
try:
    task.python_callable({'source':'orders','rows_inserted':0})
except subprocess.CalledProcessError:
    pass
else:
    raise AssertionError('DAG task hid dbt quality failure')
attempts=list(Path('/opt/airflow/logs/dbt').glob('*/*/task.json'))
assert sorted(json.loads(p.read_text())['try_number'] for p in attempts)==[1,2]
paths=Path('/opt/airflow/logs/dbt').glob('*/*/*/target/run_results.json')
results=[json.loads(p.read_text()) for p in paths]
assert len(results)==2
assert any(any(r['status']=='fail' for r in run['results']) for run in results)
print('PASS: real DAG callback, isolated dbt, shared permissions, quality failure, retry artifacts')
"""
        output = docker(
            "run",
            "--name",
            worker,
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
            "--memory",
            "1536m",
            "--cpus",
            "1",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m,uid=50000,gid=0",
            "--volumes-from",
            owner,
            "--mount",
            "type=volume,destination=/opt/airflow/logs",
            "--env",
            "AIRFLOW_HOME=/tmp/airflow",
            "--entrypoint",
            "python",
            "retailflow-airflow:validation",
            "-c",
            script,
        )
        assert "PASS: real DAG callback" in output
    finally:
        for name in (worker, owner):
            subprocess.run(["docker", "rm", "--volumes", name], capture_output=True, check=False)
