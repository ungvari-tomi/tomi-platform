"""Launch the preinstalled isolated dbt runtime from an Airflow task."""

import hashlib
import json
import logging
import os

# Fixed executable and arguments; no shell or runtime package installation.
import subprocess  # nosec B404
from pathlib import Path
from uuid import uuid4


def scheduled_build(dag_id: str, run_id: str, try_number: int, *, customers: bool = False) -> dict:
    identity = hashlib.sha256(json.dumps([dag_id, run_id, try_number]).encode()).hexdigest()
    directory = (
        Path(os.environ.get("RETAILFLOW_DBT_ARTIFACT_DIR", "/opt/airflow/logs/dbt"))
        / identity
        / uuid4().hex
    )
    directory.mkdir(parents=True, mode=0o700)
    home = directory / "home"
    home.mkdir(mode=0o700)
    logging.getLogger(__name__).info("Starting dbt build; artifacts: %s", directory)
    (directory / "task.json").write_text(
        json.dumps({"dag_id": dag_id, "run_id": run_id, "try_number": try_number}), encoding="utf-8"
    )
    # No Airflow DB/API keys, Python path, or user-site packages enter the dbt process.
    environment = {
        "PATH": "/opt/dbt/venv/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "RETAILFLOW_DUCKDB_PATH": os.environ.get(
            "RETAILFLOW_DUCKDB_PATH", "/warehouse/retailflow.duckdb"
        ),
        "RETAILFLOW_DBT_ARTIFACT_DIR": str(directory),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        "RETAILFLOW_DATASET_VERSION": os.environ.get("RETAILFLOW_DATASET_VERSION", "orders-v3"),
        "RETAILFLOW_SEED": os.environ.get("RETAILFLOW_SEED", "42"),
        "RETAILFLOW_INTERVAL_SECONDS": os.environ.get("RETAILFLOW_INTERVAL_SECONDS", "600"),
        "RETAILFLOW_CUSTOMER_PRIVACY_KEY_ID": os.environ.get(
            "RETAILFLOW_CUSTOMER_PRIVACY_KEY_ID", "local-v1"
        ),
    }
    subprocess.run(  # nosec B603
        [
            "/opt/dbt/venv/bin/python",
            "-I",
            "/opt/dbt/run.py",
            "customer-pipeline" if customers else "pipeline",
        ],
        env=environment,
        check=True,
        timeout=450,
    )
    results = list(directory.glob("*/target/run_results.json"))
    if len(results) != 1:
        raise RuntimeError("Expected exactly one dbt run-results artifact")
    records = json.loads(results[0].read_text(encoding="utf-8"))["results"]
    if not records or any(row["status"] not in ("pass", "success") for row in records):
        raise RuntimeError("dbt results include failed, skipped, or empty outcomes")
    for phase, filename in [
        ("source_freshness", "sources.json"),
        ("freshness_tests", "run_results.json"),
    ]:
        paths = list(directory.glob(f"*/{phase}/target/{filename}"))
        if len(paths) != 1:
            raise RuntimeError("Missing dbt freshness evidence")
        checks = json.loads(paths[0].read_text(encoding="utf-8"))["results"]
        expected = 1 if customers and phase == "freshness_tests" else 2
        if len(checks) != expected or any(row["status"] != "pass" for row in checks):
            raise RuntimeError("dbt freshness checks did not all pass")
    return {
        "artifact_directory": str(directory),
        "models_succeeded": sum(row["unique_id"].startswith("model.") for row in records),
        "tests_passed": sum(row["unique_id"].startswith("test.") for row in records),
        "freshness_sources_passed": 2,
        "freshness_tests_passed": 1 if customers else 2,
    }
