"""Real dbt execution against disposable version history and unmatched facts."""

import os
import subprocess
from datetime import timedelta

import duckdb
import pytest
from filelock import FileLock
from test_customer_pipeline_container import disposable_warehouse
from test_landing import MemoryStore
from test_product_catalog import snapshot

from infra.mongodb.catalog import documents
from retailflow.event_ingestion import SCHEMA as EVENT_DDL
from retailflow.product_catalog import load_manifest

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_DBT_IMAGE") != "1",
    reason="Opt in to isolated dbt validation image",
)


def test_catalog_models_preserve_facts_and_expose_corruption_and_freshness(tmp_path):
    database = tmp_path / "retailflow.duckdb"
    with duckdb.connect(str(database)):
        pass
    store = MemoryStore()
    load_manifest(store, snapshot(store), database)
    rows = documents()
    rows[0].update(
        revision=2, status="retired", updated_at=rows[0]["updated_at"] + timedelta(days=1)
    )
    load_manifest(store, snapshot(store, rows), database)
    with FileLock(str(database) + ".lock", mode=0o660):
        with duckdb.connect(str(database)) as db:
            db.execute(EVENT_DDL)
            # Include an unmatched fact; repeated order IDs belong to separate datasets.
            for dataset, product in [("a", "product-001"), ("b", "unmatched")]:
                db.execute(
                    "INSERT INTO raw.orders VALUES (?, 1, CURRENT_TIMESTAMP, 'HU', 'HUF', ?, "
                    "2, 200000, 400000, 'fixture/orders')",
                    [dataset, product],
                )
            for event, product in [("e1", "product-001"), ("e2", "unmatched")]:
                db.execute(
                    "INSERT INTO streaming.inventory_movements VALUES (?, 'inventory-v1', 42, "
                    "600, 1, CURRENT_TIMESTAMP, ?, 'warehouse-001', 3, 'restock')",
                    [event, product],
                )
    script = r"""
import json,os,stat,subprocess
from pathlib import Path
import duckdb
from filelock import FileLock
database='/warehouse/retailflow.duckdb'
assert os.getuid()==10001
assert Path('/warehouse').stat().st_gid==10001
assert stat.S_IMODE(Path('/warehouse').stat().st_mode)==0o2770
def command(action):
 return subprocess.run(['python','/app/run.py',action],capture_output=True,text=True)
def sql(text):
 with FileLock(database+'.lock',mode=0o660):
  with duckdb.connect(database) as db:
   return db.execute(text).fetchall()
baseline=command('parse')
assert baseline.returncode==0,baseline.stdout+baseline.stderr
manifests=list(Path('/artifacts').glob('*/target/manifest.json'))
manifest=json.loads(manifests[0].read_text())
models=[n for n in manifest['nodes'].values() if n.get('resource_type')=='model']
assert not any('catalog' in n.get('tags',[]) for n in models)
healthy=command('catalog-pipeline')
assert healthy.returncode==0,healthy.stdout+healthy.stderr
assert sql('SELECT COUNT(*) FROM raw.product_versions')==[(21,)]
assert sql('SELECT COUNT(*) FROM staging.current_products')==[(20,)]
assert sql("SELECT catalog_status,revision FROM staging.current_products "
           "WHERE product_id='product-001'")==[('retired',2)]
assert sql('SELECT COUNT(*), SUM(total_minor) FROM staging.catalog_orders')==[(2,800000)]
assert sql('SELECT COUNT(*), SUM(quantity_delta) FROM staging.catalog_inventory')==[(2,6)]
assert sql('SELECT COUNT(*) FROM staging.catalog_orders WHERE NOT catalog_matched')==[(1,)]
assert sql('SELECT COUNT(*) FROM staging.catalog_inventory WHERE NOT catalog_matched')==[(1,)]
sql("UPDATE raw.product_versions SET document_sha256=REPEAT('0',64) "
    "WHERE product_id='product-001' AND revision=2")
failed=command('catalog-pipeline')
assert failed.returncode!=0 and 'catalog_integrity' in failed.stdout
sql('UPDATE raw.product_versions SET document_sha256=SHA256(document_json)')
# Each freshness failure must stop before a build artifact exists in that invocation.
expressions=["CURRENT_TIMESTAMP - INTERVAL '25 hours'", "CURRENT_TIMESTAMP + INTERVAL '1 day'"]
for expression in expressions:
 before=set(Path('/artifacts').iterdir())
 sql("UPDATE ops.loaded_batches SET retrieved_at="+expression+
     " WHERE source='product-catalog/products-v1'")
 failed=command('catalog-pipeline')
 assert failed.returncode!=0,failed.stdout+failed.stderr
 created=set(Path('/artifacts').iterdir())-before
 assert len(created)==1
 assert not list(next(iter(created)).glob('target/run_results.json'))
sql("UPDATE ops.loaded_batches SET retrieved_at=CURRENT_TIMESTAMP "
    "WHERE source='product-catalog/products-v1'")
sql('DELETE FROM ops.product_batch_records')
empty=command('catalog-pipeline')
assert empty.returncode!=0 and 'catalog_freshness' in empty.stdout
assert sql('SELECT COUNT(*) FROM raw.orders')==[(2,)]
assert sql('SELECT COUNT(*) FROM streaming.inventory_movements')==[(2,)]
print('PASS catalog revisions, fact grain, unmatched rows, visible corruption and dbt freshness')
"""
    with disposable_warehouse(database) as owner:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--volumes-from",
                owner,
                "--mount",
                "type=volume,target=/artifacts",
                "--tmpfs",
                "/tmp:rw,nosuid,size=128m",
                "--entrypoint",
                "python",
                "retailflow-dbt:validation",
                "-c",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PASS catalog revisions" in result.stdout
