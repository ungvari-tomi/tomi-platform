import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest

from infra.duckdb.recovery import backup
from scripts import export_warehouse


def test_corrupted_transfer_never_publishes_verified_snapshot(monkeypatch, tmp_path: Path):
    database = tmp_path / "source.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE TABLE example AS SELECT 42 AS answer")
    observation = backup(database, tmp_path / "backups")
    bundle = Path(observation["backup"])

    def transfer(*arguments):
        if arguments[0] == "exec":
            return json.dumps({**observation, "backup": "/backups/complete"})
        shutil.copytree(bundle, arguments[-1])
        with (Path(arguments[-1]) / "warehouse.duckdb").open("ab") as stream:
            stream.write(b"damaged transfer")
        return ""

    monkeypatch.setattr(export_warehouse, "docker", transfer)
    destination = tmp_path / "snapshots"
    with pytest.raises(ValueError, match="checksum"):
        export_warehouse.export_snapshot(destination)
    assert not list(destination.glob("*/snapshot.json"))
    assert not list(destination.glob("*/retailflow.duckdb"))


@pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_DUCKDB_IMAGE") != "1",
    reason="Opt-in real warehouse runtime image required",
)
def test_repeated_exports_are_independent_while_previous_snapshot_is_open(tmp_path: Path):
    name = "retailflow-export-test-" + uuid4().hex
    destination = tmp_path / "snapshots with spaces"

    def docker(*args):
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, check=False, timeout=120
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    try:
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--mount",
            "type=volume,destination=/warehouse",
            "--mount",
            "type=volume,destination=/backups",
            "--env",
            # Local backup mount path, not a credential.
            # pragma: allowlist nextline secret
            "RETAILFLOW_DUCKDB_BACKUP_DIR=/backups",
            "retailflow-duckdb:validation",
            "python",
            "-c",
            "import duckdb,os,sys; c=duckdb.connect('/warehouse/retailflow.duckdb'); "
            "c.execute('CREATE SCHEMA raw; CREATE TABLE raw.orders AS SELECT 1 AS order_id; "
            "CREATE SCHEMA staging; CREATE VIEW staging.order_view AS "
            "SELECT * FROM retailflow.raw.orders'); "
            "c.close(); os.execv(sys.executable,[sys.executable,'/app/runner.py','serve'])",
        )
        first = export_warehouse.export_snapshot(destination, container=name)
        with duckdb.connect(first["snapshot"], read_only=True) as viewer:
            assert viewer.execute("SELECT * FROM raw.orders").fetchall() == [(1,)]
            assert viewer.execute("SELECT * FROM staging.order_view").fetchall() == [(1,)]
            docker(
                "exec",
                name,
                "python",
                "-c",
                "from filelock import FileLock; import duckdb; "
                "lock=FileLock('/warehouse/retailflow.duckdb.lock',mode=0o660,timeout=60); "
                "lock.acquire(); c=duckdb.connect('/warehouse/retailflow.duckdb'); "
                "c.execute('INSERT INTO raw.orders VALUES (2)'); c.close(); lock.release()",
            )
            second = export_warehouse.export_snapshot(destination, container=name)
            assert first["snapshot"] != second["snapshot"]
            assert viewer.execute("SELECT * FROM raw.orders").fetchall() == [(1,)]
            with duckdb.connect(second["snapshot"], read_only=True) as latest:
                assert latest.execute("SELECT * FROM raw.orders ORDER BY 1").fetchall() == [
                    (1,),
                    (2,),
                ]
                assert latest.execute("SELECT count(*) FROM staging.order_view").fetchone() == (2,)
        assert len(list(destination.glob("*/snapshot.json"))) == 2
        assert first["tables"] == [{"schema": "raw", "table": "orders", "rows": 1}]
        assert second["tables"] == [{"schema": "raw", "table": "orders", "rows": 2}]
    finally:
        subprocess.run(
            ["docker", "rm", "--force", "--volumes", name], capture_output=True, check=False
        )
