"""Run the isolated dbt CLI under the shared warehouse lock."""

import argparse
import json
import os
import runpy
import shutil

# Fixed executable and argument lists; no shell is invoked.
import subprocess  # nosec B404
from pathlib import Path
from uuid import uuid4

from filelock import FileLock


def execute(action: str, database: Path, artifact_root: Path, *, lock_timeout: float = 60) -> int:
    commands = {
        "check": [["debug", "--connection"], ["run-operation", "runtime_check"]],
        "parse": [["parse"]],
        "build": [["build"]],
        "freshness": [
            ["source", "freshness"],
            ["test", "--select", "tag:freshness", "--vars", '{"check_freshness": true}'],
        ],
        "pipeline": [
            ["source", "freshness"],
            ["test", "--select", "tag:freshness", "--vars", '{"check_freshness": true}'],
            ["build"],
        ],
        "reporting-pipeline": [
            ["source", "freshness"],
            [
                "test",
                "--select",
                "tag:freshness",
                "--vars",
                '{"check_freshness": true, "sales_reporting": true}',
            ],
            ["build", "--vars", '{"sales_reporting": true}'],
        ],
        "customer-pipeline": [
            ["source", "freshness", "--select", "source:customer_demo"],
            ["test", "--select", "tag:customer_freshness"],
            ["build", "--select", "tag:customers"],
        ],
        "parallel-pipeline": [
            ["source", "freshness", "--select", "source:inventory_stream source:parallel_ops"],
            ["test", "--select", "tag:parallel_freshness"],
            ["build", "--select", "tag:parallel"],
        ],
    }
    if action not in commands:
        raise ValueError("Unsupported dbt action")
    if action == "reporting-pipeline" and not os.environ.get("RETAILFLOW_REPORTING_DIR"):
        raise ValueError("Reporting publication directory is required")
    database = database.resolve()
    executable = shutil.which("dbt")
    if not executable:
        raise RuntimeError("The isolated dbt runtime is not installed")
    project = Path(__file__).resolve().parent / "project"
    invocation = artifact_root / uuid4().hex
    invocation.mkdir(parents=True, mode=0o700)
    env = dict(
        os.environ,
        RETAILFLOW_DUCKDB_PATH=str(database),
        DBT_SEND_ANONYMOUS_USAGE_STATS="false",
        DBT_TARGET_PATH=str(invocation / "target"),
        DBT_LOG_PATH=str(invocation / "logs"),
    )
    with FileLock(str(database) + ".lock", mode=0o660, timeout=lock_timeout) as warehouse_lock:
        if not database.is_file():
            raise FileNotFoundError("Refusing to create an empty warehouse for dbt")
        for index, arguments in enumerate(commands[action]):
            command_env = dict(env)
            if (
                action
                in {
                    "freshness",
                    "pipeline",
                    "customer-pipeline",
                    "parallel-pipeline",
                    "reporting-pipeline",
                }
                and index < 2
            ):
                phase = "source_freshness" if index == 0 else "freshness_tests"
                command_env["DBT_TARGET_PATH"] = str(invocation / phase / "target")
                command_env["DBT_LOG_PATH"] = str(invocation / phase / "logs")
            if action == "customer-pipeline":
                arguments = [*arguments, "--vars", '{"customer_pipeline": true}']
            if action == "parallel-pipeline":
                arguments = [*arguments, "--vars", '{"parallel_ingestion": true}']
            # Fixed CLI executable and validated action.
            result = subprocess.run(  # nosec B603
                [
                    executable,
                    *arguments,
                    "--project-dir",
                    str(project),
                    "--profiles-dir",
                    str(project),
                ],
                env=command_env,
                check=False,
                timeout=120,
            )
            if result.returncode:
                return result.returncode
        if action == "reporting-pipeline":
            # -I excludes the script directory: load this fixed baked-in module explicitly.
            publisher = runpy.run_path(str(project.parent / "reporting.py"))["publish_report"]
            publication = publisher(
                database,
                Path(os.environ["RETAILFLOW_REPORTING_DIR"]),
                warehouse_lock=warehouse_lock,
            )
            (invocation / "report-publication.json").write_text(
                json.dumps(publication, indent=2), encoding="utf-8"
            )
    print(json.dumps({"action": action, "status": "success", "artifacts": str(invocation)}))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "check",
            "parse",
            "build",
            "freshness",
            "pipeline",
            "reporting-pipeline",
            "customer-pipeline",
            "parallel-pipeline",
        ],
        default="check",
        nargs="?",
    )
    args = parser.parse_args()
    os.umask(0o007)
    raise SystemExit(
        execute(
            args.action,
            Path(os.environ.get("RETAILFLOW_DUCKDB_PATH", "/warehouse/retailflow.duckdb")),
            Path(os.environ.get("RETAILFLOW_DBT_ARTIFACT_DIR", "/artifacts")),
        )
    )


if __name__ == "__main__":
    main()
