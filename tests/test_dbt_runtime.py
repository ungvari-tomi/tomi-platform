import json
import os
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from filelock import FileLock, Timeout

from infra.dbt import run


def test_dbt_holds_shared_lock_and_propagates_cli_failure(monkeypatch, tmp_path: Path) -> None:
    database = tmp_path / "retailflow.duckdb"
    database.write_bytes(b"fixture")
    monkeypatch.setattr(run.shutil, "which", lambda _: "dbt")
    observed = []

    def cli(command, **kwargs):
        with pytest.raises(Timeout):
            FileLock(str(database) + ".lock", mode=0o660).acquire(timeout=0)
        assert kwargs["env"]["RETAILFLOW_DUCKDB_PATH"] == str(database.resolve())
        assert kwargs["env"]["DBT_SEND_ANONYMOUS_USAGE_STATS"] == "false"
        observed.append(command[1])
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(run.subprocess, "run", cli)
    assert run.execute("check", database, tmp_path / "artifacts") == 7
    assert observed == ["debug"]


def test_dbt_refuses_missing_warehouse_and_respects_loader_lock(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(run.shutil, "which", lambda _: "dbt")
    database = tmp_path / "warehouse.duckdb"
    with pytest.raises(FileNotFoundError):
        run.execute("check", database, tmp_path / "artifacts")
    assert not database.exists()
    database.write_bytes(b"fixture")
    with FileLock(str(database) + ".lock", mode=0o660):
        with pytest.raises(Timeout):
            run.execute("check", database, tmp_path / "artifacts", lock_timeout=0)


@pytest.mark.parametrize("failure_at", [0, 1])
def test_freshness_failure_stops_pipeline_before_build_under_common_lock(
    monkeypatch, tmp_path, failure_at
):
    database = tmp_path / "retailflow.duckdb"
    database.write_bytes(b"fixture")
    monkeypatch.setattr(run.shutil, "which", lambda _: "dbt")
    calls = []

    def execute(command, **kwargs):
        with pytest.raises(Timeout):
            FileLock(str(database) + ".lock", mode=0o660).acquire(timeout=0)
        assert str(database.resolve()) == kwargs["env"]["RETAILFLOW_DUCKDB_PATH"]
        calls.append((command, kwargs["env"]["DBT_TARGET_PATH"]))
        return SimpleNamespace(returncode=1 if len(calls) - 1 == failure_at else 0)

    monkeypatch.setattr(run.subprocess, "run", execute)
    assert run.execute("pipeline", database, tmp_path / "artifacts") == 1
    assert len(calls) == failure_at + 1
    assert not any(command[1] == "build" for command, _ in calls)
    assert len({path for _, path in calls}) == len(calls)


def test_dbt_lock_matches_shared_duckdb_version_and_export_hashes():
    root = Path(__file__).resolve().parents[1]
    dbt = tomllib.loads((root / "infra/dbt/uv.lock").read_text(encoding="utf-8"))
    main = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    for name in ["duckdb", "filelock"]:
        assert next(p["version"] for p in dbt["package"] if p["name"] == name) == next(
            p["version"] for p in main["package"] if p["name"] == name
        )
    package = None
    for line in (root / "infra/dbt/requirements.txt").read_text().splitlines():
        if "==" in line and not line.startswith("#"):
            name, version = line.split()[0].split("==")
            package = next(p for p in dbt["package"] if p["name"] == name)
            assert version == package["version"]
        if "--hash=" in line:
            digest = line.split("--hash=", 1)[1].strip().rstrip("\\").strip()
            assert package and digest in {
                item["hash"]
                for item in [*package.get("wheels", []), package.get("sdist", {})]
                if "hash" in item
            }


def test_customer_pipeline_limits_selection_and_keeps_all_phases_locked(monkeypatch, tmp_path):
    database = tmp_path / "retailflow.duckdb"
    database.write_bytes(b"fixture")
    monkeypatch.setattr(run.shutil, "which", lambda _: "dbt")
    calls = []

    def execute(command, **kwargs):
        with pytest.raises(Timeout):
            FileLock(str(database) + ".lock", mode=0o660).acquire(timeout=0)
        assert command[command.index("--vars") + 1] == '{"customer_pipeline": true}'
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(run.subprocess, "run", execute)
    assert run.execute("customer-pipeline", database, tmp_path / "artifacts") == 0
    assert [c[c.index("--select") + 1] for c in calls] == [
        "source:customer_demo",
        "tag:customer_freshness",
        "tag:customers",
    ]


@pytest.mark.skipif(os.environ.get("RETAILFLOW_TEST_DBT_IMAGE") != "1", reason="Opt-in dbt image")
def test_real_dbt_checks_and_parses_without_creating_business_models():
    name = "retailflow-dbt-test-" + uuid4().hex
    image = "retailflow-dbt:validation"
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
        "/tmp:rw,noexec,nosuid,size=128m,uid=10001,gid=10001",
    ]

    def docker(*args, check=True):
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, check=False, timeout=180
        )
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    try:
        docker(
            "run",
            "--name",
            name,
            *controls,
            "--mount",
            "type=volume,destination=/warehouse",
            "--mount",
            "type=volume,destination=/artifacts",
            "--entrypoint",
            "python",
            image,
            "-c",
            "import duckdb; c=duckdb.connect('/warehouse/retailflow.duckdb'); "
            "c.execute('CREATE SCHEMA raw; CREATE TABLE raw.orders AS SELECT 1 AS order_id; "
            "CREATE TABLE raw.exchange_rates AS SELECT 366.33 AS rate'); c.close()",
        )
        output = docker("run", "--rm", *controls, "--volumes-from", name, image, "check").stdout
        assert '"status": "success"' in output
        assert "Verified raw source orders: 1 rows" in output
        assert "Verified raw source exchange_rates: 1 rows" in output
        docker("run", "--rm", *controls, "--volumes-from", name, image, "parse")
        state = docker(
            "run",
            "--rm",
            *controls,
            "--volumes-from",
            name,
            "--entrypoint",
            "python",
            image,
            "-c",
            "import duckdb,json; c=duckdb.connect('/warehouse/retailflow.duckdb'); "
            'print(json.dumps(c.execute("SELECT table_schema,table_name FROM '
            'information_schema.tables ORDER BY 1,2").fetchall())); '
            "c.execute('DROP TABLE raw.exchange_rates'); c.close()",
        )
        assert json.loads(state.stdout) == [["raw", "exchange_rates"], ["raw", "orders"]]
        failed = docker(
            "run", "--rm", *controls, "--volumes-from", name, image, "check", check=False
        )
        assert failed.returncode != 0
        assert "exchange_rates" in failed.stdout
    finally:
        subprocess.run(["docker", "rm", "--volumes", name], capture_output=True, check=False)
