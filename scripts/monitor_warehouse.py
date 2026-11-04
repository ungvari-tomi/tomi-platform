"""Read-only SQL availability under the common lock; freshness belongs to dbt."""

import json
import os
from pathlib import Path

import duckdb
from filelock import FileLock, Timeout


def collect(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError("Warehouse is unavailable")
    with FileLock(str(path) + ".lock", mode=0o660, timeout=5):
        with duckdb.connect(
            str(path),
            read_only=True,
            config={"threads": 1, "memory_limit": "256MB", "enable_external_access": False},
        ) as connection:
            if connection.execute("SELECT 1").fetchone() != (1,):
                raise RuntimeError("Warehouse SQL check failed")
    return {"status": "ok"}


if __name__ == "__main__":
    warehouse = Path(os.environ.get("RETAILFLOW_DUCKDB_PATH", "/warehouse/retailflow.duckdb"))
    try:
        report = collect(warehouse)
    except Timeout:
        report = {"status": "busy"}
    print(json.dumps(report))
