"""Opt-in disposable container check; never touches the managed warehouse volume."""

import json
import os
import subprocess
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_DUCKDB_IMAGE") != "1",
    reason="Build retailflow-duckdb:validation and opt in to Docker image testing",
)


def test_automatic_backup_survives_runtime_and_restores_in_new_container() -> None:
    name = f"retailflow-backup-test-{uuid4().hex}"
    image = "retailflow-duckdb:validation"

    def docker(*args: str) -> str:
        return subprocess.run(
            ["docker", *args], check=True, capture_output=True, text=True, timeout=90
        ).stdout

    controls = [
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--memory",
        "512m",
    ]
    try:
        docker(
            "run",
            "--name",
            name,
            *controls,
            "--mount",
            "type=volume,destination=/warehouse",
            "--mount",
            "type=volume,destination=/backups",
            "--env",
            # Local backup mount path, not a credential.
            # pragma: allowlist nextline secret
            "RETAILFLOW_DUCKDB_BACKUP_DIR=/backups",
            image,
            "python",
            "-c",
            "import duckdb,subprocess,time,signal\n"
            "from pathlib import Path\n"
            "p='/warehouse/retailflow.duckdb'\n"
            "with duckdb.connect(p) as c:\n"
            " c.execute('CREATE TABLE evidence AS SELECT 42 AS value')\n"
            "with Path('/warehouse/runtime.log').open('w') as log:\n"
            " child=subprocess.Popen(['python','/app/runner.py','serve'],stdout=log,stderr=log)\n"
            " try:\n"
            "  for _ in range(300):\n"
            "   if list(Path('/backups').glob('*/manifest.json')): break\n"
            "   if child.poll() is not None: raise RuntimeError('Runtime exited early')\n"
            "   time.sleep(0.1)\n"
            "  else: raise TimeoutError('Automatic backup not published')\n"
            " finally:\n"
            "  child.send_signal(signal.SIGTERM)\n"
            "  child.wait(timeout=10)\n"
            " assert child.returncode == 0\n"
            "print('automatic-backup-ok')\n",
        )
        output = docker(
            "run",
            "--rm",
            *controls,
            "--volumes-from",
            f"{name}:rw",
            image,
            "python",
            "-c",
            "from pathlib import Path\n"
            "from recovery import restore\n"
            "import duckdb,json\n"
            "bundle=next(Path('/backups').glob('*/manifest.json')).parent\n"
            "target=Path('/warehouse/recovery-drill.duckdb')\n"
            "result=restore(bundle,target)\n"
            "with duckdb.connect(str(target),read_only=True) as c:\n"
            " assert c.execute('SELECT * FROM evidence').fetchall()==[(42,)]\n"
            "print(json.dumps(result))\n",
        )
        assert json.loads(output)["status"] == "verified"
    finally:
        subprocess.run(["docker", "rm", "--volumes", name], capture_output=True, check=False)


def test_non_root_image_persists_database_between_containers() -> None:
    name = f"retailflow-duckdb-test-{uuid4().hex}"
    image = "retailflow-duckdb:validation"
    controls = [
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--memory",
        "512m",
        "--cpus",
        "1",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m,uid=10001,gid=10001",
    ]

    def docker(*args: str) -> str:
        return subprocess.run(
            ["docker", *args], check=True, capture_output=True, text=True, timeout=60
        ).stdout

    try:
        docker(
            "run",
            "--name",
            name,
            *controls,
            "--mount",
            "type=volume,destination=/warehouse",
            image,
            "python",
            "-c",
            "import errno, os\n"
            "from runner import check_database\n"
            "from pathlib import Path\n"
            "assert os.getuid() == 10001\n"
            "try:\n"
            "    Path('/var/tmp/retailflow-probe').write_text('denied')\n"
            "except OSError as error:\n"
            "    assert error.errno == errno.EROFS\n"
            "else:\n"
            "    raise AssertionError('Root filesystem was writable')\n"
            "print(check_database(Path('/warehouse/retailflow.duckdb')))\n",
        )
        details = json.loads(docker("inspect", name))[0]
        assert details["Config"]["User"] == "10001:10001"
        assert details["HostConfig"]["ReadonlyRootfs"] is True
        assert "ALL" in details["HostConfig"]["CapDrop"]
        assert "no-new-privileges:true" in details["HostConfig"]["SecurityOpt"]
        assert details["HostConfig"]["Memory"] == 512 * 1024**2
        assert details["HostConfig"]["NanoCpus"] == 10**9
        # New process/container, same anonymous test volume, read-only database open.
        result = json.loads(
            docker(
                "run",
                "--rm",
                *controls,
                "--volumes-from",
                f"{name}:rw",
                image,
                "python",
                "/app/runner.py",
                "check",
            )
        )
        assert result["status"] == "ok"
    finally:
        # Only our UUID-named test container and its anonymous volume are removed.
        subprocess.run(["docker", "rm", "--volumes", name], capture_output=True, check=False)


@pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_AIRFLOW_IMAGE") != "1",
    reason="Both updated warehouse and Airflow validation images are required",
)
def test_airflow_user_loads_into_shared_warehouse_volume() -> None:
    name = f"retailflow-warehouse-share-test-{uuid4().hex}"
    controls = [
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
    ]

    def docker(*args: str) -> str:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, check=True, timeout=60
        ).stdout

    try:
        docker(
            "run",
            "--name",
            name,
            *controls,
            "--mount",
            "type=volume,destination=/warehouse",
            "retailflow-duckdb:validation",
            "python",
            "-c",
            "import os; from pathlib import Path; from runner import permissions,check_database; "
            "os.umask(0o007); path=Path('/warehouse/retailflow.duckdb'); "
            "permissions(path); print(check_database(path))",
        )
        script = """
import hashlib,json,os
from pathlib import Path
from retailflow.warehouse import load_manifest
assert os.getuid() == 50000 and 10001 in os.getgroups()
manifest='orders/ingestion_date=2026-10-31/batch=shared/metadata.json'
payload=b'order_id,created_at,country,currency,product_id,quantity,unit_price_minor,total_minor\\n1,2025-01-01T00:00:00Z,DE,EUR,product-001,1,1000,1000\\n'
name=manifest.rsplit('/',1)[0]+'/data.csv'
metadata={'manifest_version':1,'source':'orders','format':'csv','encoding':'utf-8',
          'payload_blob':name,'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest(),
          'retrieved_at':'2026-10-31T12:00:00Z','source_metadata':{
              'dataset_version':'orders-v2','seed':42,'interval_seconds':600,'row_count':1}}
blobs={manifest:json.dumps(metadata).encode(),name:payload}
class Store:
    def read(self,name):return blobs[name]
database=Path('/warehouse/retailflow.duckdb')
assert load_manifest(Store(),manifest,database)['rows_inserted'] == 1
assert load_manifest(Store(),manifest,database)['already_loaded']
print('PASS: Airflow UID loaded and replayed a batch on the shared protected directory')
"""
        docker(
            "run",
            "--rm",
            *controls,
            "--user",
            "50000:0",
            "--group-add",
            "10001",
            "--volumes-from",
            f"{name}:rw",
            "--entrypoint",
            "python",
            "retailflow-airflow:validation",
            "-c",
            script,
        )
        result = json.loads(
            docker(
                "run",
                "--rm",
                *controls,
                "--volumes-from",
                f"{name}:rw",
                "retailflow-duckdb:validation",
                "python",
                "/app/runner.py",
                "report",
            )
        )
        assert result["raw.orders"] == 1 and result["ops.loaded_batches"] == 1
    finally:
        subprocess.run(["docker", "rm", "--volumes", name], capture_output=True, check=False)
