"""Read aggregate freshness under the warehouse's common lock; usable over stdin."""

import json
import os
import sys
from datetime import UTC
from pathlib import Path

import duckdb
from filelock import FileLock, Timeout


def collect(path: Path, identity: dict) -> dict:
    if not path.is_file():
        raise FileNotFoundError("Warehouse is unavailable")
    with FileLock(str(path) + ".lock", mode=0o660, timeout=5):
        with duckdb.connect(
            str(path),
            read_only=True,
            config={"threads": 1, "memory_limit": "256MB", "enable_external_access": False},
        ) as connection:
            latest = connection.execute(
                """SELECT max(o.created_at) FROM raw.orders o
                   JOIN ops.datasets d USING (dataset_id)
                   WHERE d.dataset_version = ? AND d.seed = ? AND d.interval_seconds = ?""",
                [identity["dataset_version"], identity["seed"], identity["interval_seconds"]],
            ).fetchone()[0]
            rates = connection.execute(
                """SELECT quote, max(rate_date) FROM raw.exchange_rates
                   WHERE provider = ? AND base = ? GROUP BY quote""",
                ["ECB", "EUR"],
            ).fetchall()
    return {
        "latest_order_at": latest.astimezone(UTC).isoformat() if latest else None,
        "fx_dates": {quote: day.isoformat() for quote, day in rates},
    }


if __name__ == "__main__":
    warehouse = Path(os.environ.get("RETAILFLOW_DUCKDB_PATH", "/warehouse/retailflow.duckdb"))
    try:
        report = collect(warehouse, json.loads(sys.argv[1]))
    except Timeout:
        report = {"status": "busy"}
    print(json.dumps(report))
