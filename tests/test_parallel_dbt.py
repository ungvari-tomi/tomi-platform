"""Run opt-in parallel models via the real isolated, lock-aware dbt runtime."""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
from test_customer_pipeline_container import disposable_warehouse
from test_customer_privacy import MemoryStore

from retailflow.api import EPOCH
from retailflow.event_contracts import generate
from retailflow.event_ingestion import ingest
from retailflow.event_runtime import TOPIC


@pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_AIRFLOW_IMAGE") != "1",
    reason="Opt in to the disposable Airflow/dbt validation runtime",
)
def test_parallel_dbt_and_existing_batch_models_remain_separate(tmp_path):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "retailflow.duckdb"
    duckdb.connect(str(database)).close()
    sequence = int((datetime.now(UTC) - EPOCH).total_seconds()) // 600 + 1
    store = MemoryStore()
    ingest(store, generate("inventory-v1", sequence), database, topic=TOPIC, partition=0, offset=0)
    ingest(store, generate("shipments-v1", sequence), database)
    script = r"""
import subprocess
import duckdb
from filelock import FileLock
from retailflow.transformation import scheduled_build
result=scheduled_build('retailflow_parallel_analytics','healthy',1,parallel=True)
assert result['models_succeeded']==4 and result['tests_passed']>=14
assert result['freshness_sources_passed']==2 and result['freshness_tests_passed']==1
path='/warehouse/retailflow.duckdb'
with FileLock(path+'.lock',mode=0o660):
 with duckdb.connect(path) as db:
  assert db.execute('SELECT COUNT(*) FROM staging.stg_inventory_movements').fetchone()==(1,)
  assert db.execute('SELECT COUNT(*) FROM staging.latest_shipment_status').fetchone()==(1,)
  query="SELECT COUNT(*) FROM information_schema.views WHERE table_name='orders_eur'"
  assert db.execute(query).fetchone()==(0,)
  db.execute('UPDATE streaming.inventory_movements SET quantity_delta=0')
try:
 scheduled_build('retailflow_parallel_analytics','corrupt',1,parallel=True)
except subprocess.CalledProcessError:
 print('PASS parallel models and visible corruption failure')
else:
 raise AssertionError('Invalid inventory movement was silently accepted')
"""
    with disposable_warehouse(database) as owner:
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
            "--volumes-from",
            owner,
            "--mount",
            "type=volume,target=/opt/airflow/logs",
            "--tmpfs",
            "/tmp:rw,nosuid,size=128m",
            "-e",
            "RETAILFLOW_DUCKDB_PATH=/warehouse/retailflow.duckdb",
        ]
        for source, target in [
            (root / "infra/dbt/project", "/opt/dbt/project"),
            (root / "infra/dbt/run.py", "/opt/dbt/run.py"),
            (root / "retailflow/transformation.py", "/opt/airflow/retailflow/transformation.py"),
        ]:
            command += ["--mount", f"type=bind,source={source},target={target},readonly"]
        command += ["--entrypoint", "python", "retailflow-airflow:validation", "-c", script]
        result = subprocess.run(command, capture_output=True, text=True, timeout=240)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PASS parallel models" in result.stdout
