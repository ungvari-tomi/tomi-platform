import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from retailflow import transformation


def test_build_sanitizes_environment_and_returns_small_summary(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RETAILFLOW_DBT_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setenv("AIRFLOW_DB_PASSWORD", "test-only-not-a-secret")
    monkeypatch.setenv("PYTHONPATH", "must-not-enter-dbt")

    def execute(command, *, env, check, timeout):
        assert command == ["/opt/dbt/venv/bin/python", "-I", "/opt/dbt/run.py", "pipeline"]
        assert check is True and timeout == 450
        assert "AIRFLOW_DB_PASSWORD" not in env and "PYTHONPATH" not in env
        assert env["PYTHONNOUSERSITE"] == "1"
        target = Path(env["RETAILFLOW_DBT_ARTIFACT_DIR"]) / "invocation/target"
        target.mkdir(parents=True)
        (target / "run_results.json").write_text(
            json.dumps(
                {
                    "results": [
                        {"status": "success", "unique_id": "model.retailflow.stg_orders"},
                        {"status": "pass", "unique_id": "test.retailflow.quality"},
                    ]
                }
            )
        )
        for phase, filename in [
            ("source_freshness", "sources.json"),
            ("freshness_tests", "run_results.json"),
        ]:
            path = target.parent / phase / "target"
            path.mkdir(parents=True)
            (path / filename).write_text(json.dumps({"results": [{"status": "pass"}] * 2}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(transformation.subprocess, "run", execute)
    result = transformation.scheduled_build("retailflow_orders", "manual__a/b", 1)
    assert result["models_succeeded"] == 1 and result["tests_passed"] == 1
    directory = Path(result["artifact_directory"])
    assert directory.is_relative_to(tmp_path)
    assert json.loads((directory / "task.json").read_text())["run_id"] == "manual__a/b"


@pytest.mark.parametrize(
    "error", [subprocess.CalledProcessError(1, "dbt"), subprocess.TimeoutExpired("dbt", 150)]
)
def test_build_failure_propagates_and_retry_keeps_previous_artifacts(monkeypatch, tmp_path, error):
    monkeypatch.setenv("RETAILFLOW_DBT_ARTIFACT_DIR", str(tmp_path))

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(transformation.subprocess, "run", fail)
    for attempt in [1, 2]:
        with pytest.raises(type(error)):
            transformation.scheduled_build("orders", "same-run", attempt)
    attempts = [json.loads(p.read_text())["try_number"] for p in tmp_path.glob("*/*/task.json")]
    assert sorted(attempts) == [1, 2]


@pytest.mark.parametrize("status", ["fail", "skipped", "error"])
def test_build_rejects_unsuccessful_artifact_even_if_cli_returns_zero(
    monkeypatch, tmp_path, status
):
    monkeypatch.setenv("RETAILFLOW_DBT_ARTIFACT_DIR", str(tmp_path))

    def execute(*args, **kwargs):
        target = Path(kwargs["env"]["RETAILFLOW_DBT_ARTIFACT_DIR"]) / "child/target"
        target.mkdir(parents=True)
        (target / "run_results.json").write_text(json.dumps({"results": [{"status": status}]}))

    monkeypatch.setattr(transformation.subprocess, "run", execute)
    with pytest.raises(RuntimeError, match="failed, skipped, or empty"):
        transformation.scheduled_build("orders", "run", 1)
