"""Exercise the developer command with local edits and disposable warehouse storage."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_DBT_IMAGE") != "1"
    or not (shutil.which("pwsh") or shutil.which("powershell")),
    reason="Opt-in dbt Docker image and PowerShell required",
)

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=180, check=False
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def develop(action: str, project: Path, artifacts: Path, volume: str):
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts/dbt-dev.ps1"),
            action,
            "-ProjectDirectory",
            str(project),
            "-ArtifactDirectory",
            str(artifacts),
            "-WarehouseVolume",
            volume,
            "-Image",
            "retailflow-dbt:validation",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_local_model_edits_build_without_rebuilding_image(tmp_path: Path):
    project = tmp_path / "project with spaces"
    artifacts = tmp_path / "local artifacts"
    # This helper test uses a tiny standalone project, independent of business models.
    project.mkdir()
    (project / "models").mkdir()
    for filename in ("dbt_project.yml", "profiles.yml"):
        shutil.copy2(ROOT / "infra/dbt/project" / filename, project / filename)
    shutil.copy2(ROOT / "infra/dbt/project/models/sources.yml", project / "models/sources.yml")
    model = project / "models/dev_probe.sql"
    (project / "models/dev_probe.yml").write_text(
        "version: 2\nmodels:\n  - name: dev_probe\n    columns:\n"
        "      - name: order_id\n        data_tests: [not_null]\n",
        encoding="utf-8",
    )
    name = "retailflow-dbt-dev-test-" + uuid4().hex
    try:
        docker(
            "run",
            "--name",
            name,
            "--network",
            "none",
            "--mount",
            "type=volume,destination=/warehouse",
            "--entrypoint",
            "python",
            "retailflow-dbt:validation",
            "-c",
            "import duckdb; c=duckdb.connect('/warehouse/retailflow.duckdb'); "
            "c.execute('CREATE SCHEMA raw; CREATE TABLE raw.orders AS SELECT 123 AS order_id'); "
            "c.close()",
        )
        mounts = json.loads(docker("inspect", name, "--format", "{{json .Mounts}}").stdout)
        volume = next(m["Name"] for m in mounts if m["Destination"] == "/warehouse")
        for revision in (1, 2):
            model.write_text(
                "{{ config(materialized='table') }}\n"
                f"select order_id, {revision} as revision "
                "from {{ source('retailflow_raw', 'orders') }}\n",
                encoding="utf-8",
            )
            result = develop("build", project, artifacts, volume)
            assert result.returncode == 0, result.stdout + result.stderr
            observed = docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--volumes-from",
                name,
                "--entrypoint",
                "python",
                "retailflow-dbt:validation",
                "-c",
                "import duckdb,json; "
                "c=duckdb.connect('/warehouse/retailflow.duckdb',read_only=True); "
                "print(json.dumps(c.execute('select * from staging.dev_probe').fetchall()))",
            )
            assert json.loads(observed.stdout) == [[123, revision]]
        results = list(artifacts.glob("*/target/run_results.json"))
        assert len(results) == 2
        assert all(
            result["status"] in ("success", "pass")
            for path in results
            for result in json.loads(path.read_text())["results"]
        )
        assert not (project / "target").exists()
        assert not (project / "logs").exists()
    finally:
        subprocess.run(["docker", "rm", "--volumes", name], capture_output=True, check=False)


def test_missing_volume_fails_without_creating_it(tmp_path: Path):
    volume = "retailflow-dbt-missing-" + uuid4().hex
    result = develop("parse", ROOT / "infra/dbt/project", tmp_path / "artifacts", volume)
    assert result.returncode != 0
    assert docker("volume", "inspect", volume, check=False).returncode != 0
    assert not (tmp_path / "artifacts").exists()
