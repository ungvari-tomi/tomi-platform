"""Run the isolated dbt CLI under the shared warehouse lock."""

import argparse
import json
import os
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
    }
    if action not in commands:
        raise ValueError("Unsupported dbt action")
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
    with FileLock(str(database) + ".lock", mode=0o660, timeout=lock_timeout):
        if not database.is_file():
            raise FileNotFoundError("Refusing to create an empty warehouse for dbt")
        for arguments in commands[action]:
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
                env=env,
                check=False,
                timeout=120,
            )
            if result.returncode:
                return result.returncode
    print(json.dumps({"action": action, "status": "success", "artifacts": str(invocation)}))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "parse", "build"], default="check", nargs="?")
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
