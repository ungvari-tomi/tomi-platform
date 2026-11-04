"""Run real dbt freshness against current, empty, stale and future disposable data."""

import os
import subprocess
from uuid import uuid4

import pytest


@pytest.mark.skipif(os.environ.get("RETAILFLOW_TEST_DBT_IMAGE") != "1", reason="Opt-in dbt image")
def test_real_dbt_freshness_checks_current_identity_and_each_fx_pair():
    owner, worker = "fresh-owner-" + uuid4().hex, "fresh-worker-" + uuid4().hex

    def docker(*args):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=240)
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
            "type=volume,target=/warehouse",
            "--entrypoint",
            "python",
            "retailflow-dbt:validation",
            "-c",
            "import duckdb; duckdb.connect('/warehouse/retailflow.duckdb').close()",
        )
        script = '''
import json,subprocess
from pathlib import Path
import duckdb
from filelock import FileLock
path='/warehouse/retailflow.duckdb'
fixture="""
CREATE SCHEMA IF NOT EXISTS raw; CREATE SCHEMA IF NOT EXISTS ops;
CREATE OR REPLACE TABLE ops.datasets AS SELECT * FROM (VALUES
 ('active','orders-v3',42,600),('legacy','orders-v2',42,600))
 d(dataset_id,dataset_version,seed,interval_seconds);
CREATE OR REPLACE TABLE raw.orders AS
 SELECT 'active' AS dataset_id,1 AS order_id,current_timestamp AS created_at
 UNION ALL SELECT 'legacy',1,current_timestamp + interval '2 hours';
CREATE OR REPLACE TABLE raw.exchange_rates AS SELECT
 'ECB' AS provider,'EUR' AS base,quote,current_date - 3 AS rate_date
 FROM (VALUES ('HUF'),('PLN'),('CZK'),('RON'),('GBP'),('CHF'),('SEK'),('NOK'),('DKK')) q(quote);
CREATE OR REPLACE TABLE ops.loaded_batches AS SELECT quote AS manifest_path,
 'exchange-rates' AS source,current_timestamp AS loaded_at FROM raw.exchange_rates;
CREATE OR REPLACE TABLE ops.fx_batch_records AS SELECT quote AS manifest_path,
 provider,base,quote,rate_date,rate_date AS requested_date FROM raw.exchange_rates;
"""
cases=[
 ('healthy','',False),
 ('stale_orders',"UPDATE raw.orders SET created_at=current_timestamp-interval '31 minutes' "
  "WHERE dataset_id='active'",True),
 ('empty_orders',"DELETE FROM raw.orders WHERE dataset_id='active'",True),
 ('stale_fx_ingestion',"UPDATE ops.loaded_batches "
  "SET loaded_at=current_timestamp-interval '31 hours'",True),
 ('stale_one_pair',"UPDATE ops.loaded_batches SET loaded_at=current_timestamp-interval '31 hours' "
  "WHERE manifest_path='HUF'",True),
 ('missing_pair',"DELETE FROM raw.exchange_rates WHERE quote='HUF'",True),
 ('old_rate',"UPDATE raw.exchange_rates SET rate_date=current_date-8 WHERE quote='HUF'",True),
 ('future_rate',"UPDATE raw.exchange_rates SET rate_date=current_date+1 WHERE quote='HUF'",True),
 ('future_order',"UPDATE raw.orders SET created_at=current_timestamp+interval '2 minutes' "
  "WHERE dataset_id='active'",True),
]
tables=['raw.orders','raw.exchange_rates','ops.datasets','ops.loaded_batches','ops.fx_batch_records']
for name,change,expected_failure in cases:
 with FileLock(path+'.lock',mode=0o660,timeout=10):
  with duckdb.connect(path) as c:
   c.execute(fixture)
   if change: c.execute(change)
   before={table:c.execute('SELECT * FROM '+table).fetchall() for table in tables}
 result=subprocess.run(['python','/app/run.py','freshness'],capture_output=True,text=True,
                       env=dict(__import__('os').environ,RETAILFLOW_DBT_ARTIFACT_DIR='/artifacts/'+name),timeout=120)
 assert (result.returncode != 0)==expected_failure,(name,result.stdout,result.stderr)
 with FileLock(path+'.lock',mode=0o660,timeout=10):
  with duckdb.connect(path,read_only=True) as c:
   assert all(c.execute('SELECT * FROM '+table).fetchall()==rows for table,rows in before.items())
 if name=='stale_one_pair':
  sources=list(Path('/artifacts/'+name).glob('*/source_freshness/target/sources.json'))
  assert len(sources)==1
  assert all(row['status']=='pass' for row in json.loads(sources[0].read_text())['results'])
  tests=list(Path('/artifacts/'+name).glob('*/freshness_tests/target/run_results.json'))
  assert len(tests)==1
  assert any(row['status']=='fail' for row in json.loads(tests[0].read_text())['results'])
 print('PASS freshness scenario:',name,flush=True)
'''
        output = docker(
            "run",
            "--name",
            worker,
            "--network",
            "none",
            "--read-only",
            "--volumes-from",
            owner,
            "--mount",
            "type=volume,target=/artifacts",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=128m,uid=10001,gid=10001",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--entrypoint",
            "python",
            "retailflow-dbt:validation",
            "-c",
            script,
        )
        assert output.count("PASS freshness scenario:") == 9
    finally:
        for name in [worker, owner]:
            subprocess.run(
                ["docker", "rm", "--force", "--volumes", name], capture_output=True, timeout=60
            )
