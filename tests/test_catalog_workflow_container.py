"""Real SDK, isolated driver and scheduler-identity loading/dbt in disposable storage."""

import base64
import json
import os
import subprocess

import duckdb
import pytest
from test_customer_pipeline_container import disposable_warehouse
from test_landing import MemoryStore
from test_product_catalog import snapshot

from retailflow.event_ingestion import SCHEMA
from retailflow.warehouse import DDL


@pytest.mark.skipif(os.environ.get("RETAILFLOW_TEST_AIRFLOW_IMAGE") != "1", reason="Opt-in image")
def test_real_catalog_sdk_loader_and_isolated_dbt(tmp_path):
    database = tmp_path / "retailflow.duckdb"
    with duckdb.connect(str(database)) as db:
        db.execute(DDL)
        db.execute(SCHEMA)
    store = MemoryStore()
    manifest = snapshot(store)
    fixture = tmp_path / "landing.json"
    fixture.write_text(
        json.dumps(
            {
                "manifest": manifest,
                "blobs": {k: base64.b64encode(v).decode() for k, v in store.blobs.items()},
            }
        )
    )
    script = r"""
import base64,importlib.util,json,os,runpy,subprocess
from pathlib import Path
assert os.getuid()==50000 and 10001 in os.getgroups()
assert importlib.util.find_spec('pymongo') is None
r=subprocess.run(['/opt/catalog/venv/bin/python','-I','-c',
 'import pymongo;from infra.mongodb.extract import encode;assert encode([])==b"[]"'],
 capture_output=True,text=True)
assert r.returncode==0,r.stdout+r.stderr
namespace=runpy.run_path('/opt/airflow/dags/retailflow_catalog.py')
d=namespace['catalog']()
assert d.is_paused_upon_creation and d.max_active_runs==1 and not d.catchup
assert d.on_failure_callback is namespace['dag_failure']
assert d.get_task('extract').downstream_task_ids=={'load'}
assert d.get_task('load').downstream_task_ids=={'dbt_build'}
assert all(t.retries==2 and not t.on_failure_callback for t in d.tasks)
fixture=json.loads(Path('/warehouse/landing.json').read_text())
class Store:
 def read(self,name):return base64.b64decode(fixture['blobs'][name])
 def close(self):pass
import retailflow.catalog_pipeline as pipeline
pipeline.BlobStore=Store
assert pipeline.scheduled_load(fixture['manifest'])['rows_inserted']==20
assert pipeline.scheduled_load(fixture['manifest'])['already_loaded']
from retailflow.transformation import scheduled_build
result=scheduled_build('retailflow_catalog','disposable',1,catalog=True)
assert result['models_succeeded']==4 and result['tests_passed']>=15
assert result['freshness_sources_passed']==result['freshness_tests_passed']==1
print('PASS real SDK, isolated MongoDB driver, scheduler load/replay and catalog dbt pipeline')
"""
    with disposable_warehouse(database) as owner:
        copied = subprocess.run(
            ["docker", "cp", str(fixture), owner + ":/warehouse/landing.json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert copied.returncode == 0, copied.stderr
        result = subprocess.run(
            [
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
                "RETAILFLOW_CATALOG_ENABLED=true",
                "-e",
                "RETAILFLOW_DUCKDB_PATH=/warehouse/retailflow.duckdb",
                "--entrypoint",
                "python",
                "retailflow-airflow:validation",
                "-c",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=240,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PASS real SDK" in result.stdout
