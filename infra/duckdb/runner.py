"""Initialize an empty warehouse file and keep the managed runtime available."""

import argparse
import json
import os
import signal
from pathlib import Path
from threading import Event

import duckdb
from filelock import FileLock, Timeout

try:
    from .recovery import backup, restore
except ImportError:  # Direct container script execution.
    from recovery import backup, restore


def check_database(path: Path, *, read_only: bool = False) -> dict[str, str]:
    lock = FileLock(str(path) + ".lock", mode=0o660)
    try:
        lock.acquire(timeout=0 if read_only else 60)
    except Timeout:
        # A cooperating loader owns the file; do not race its write connection.
        return {"status": "busy", "database": str(path)}
    try:
        return probe_database(path, read_only=read_only)
    finally:
        lock.release()


def probe_database(path: Path, *, read_only: bool = False) -> dict[str, str]:
    threads = int(os.environ.get("RETAILFLOW_DUCKDB_THREADS", "1"))
    if threads < 1:
        raise ValueError("DuckDB threads must be positive")
    with duckdb.connect(
        str(path),
        read_only=read_only,
        config={
            "threads": threads,
            "memory_limit": os.environ.get("RETAILFLOW_DUCKDB_MEMORY_LIMIT", "256MB"),
        },
    ) as connection:
        if connection.execute("SELECT 1").fetchone() != (1,):
            raise RuntimeError("DuckDB SQL probe failed")
        version = connection.execute("SELECT version()").fetchone()[0]
    return {"status": "ok", "database": str(path), "duckdb_version": version}


def permissions(path: Path) -> dict:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o2770)
    if path.exists() and path.stat().st_uid == os.getuid():
        path.chmod(0o660)
    return {"directory": str(directory), "shared_group": os.getgid(), "mode": "2770"}


def report(path: Path) -> dict:
    with FileLock(str(path) + ".lock", mode=0o660, timeout=60):
        with duckdb.connect(str(path), read_only=True) as connection:
            counts = {}
            queries = {
                ("raw", "orders"): "SELECT count(*) FROM raw.orders",
                ("raw", "exchange_rates"): "SELECT count(*) FROM raw.exchange_rates",
                ("ops", "loaded_batches"): "SELECT count(*) FROM ops.loaded_batches",
            }
            for (schema, table), query in queries.items():
                exists = connection.execute(
                    """SELECT count(*) FROM information_schema.tables
                       WHERE table_schema=? AND table_name=?""",
                    [schema, table],
                ).fetchone()[0]
                counts[f"{schema}.{table}"] = (
                    connection.execute(query).fetchone()[0] if exists else 0
                )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["serve", "check", "permissions", "report", "backup", "restore"]
    )
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--target", type=Path)
    args = parser.parse_args()
    path = Path(os.environ.get("RETAILFLOW_DUCKDB_PATH", "/warehouse/retailflow.duckdb"))
    os.umask(0o007)
    backup_directory = os.environ.get("RETAILFLOW_DUCKDB_BACKUP_DIR")
    if args.action == "backup":
        if not backup_directory:
            parser.error("RETAILFLOW_DUCKDB_BACKUP_DIR is required")
        print(json.dumps(backup(path, Path(backup_directory))))
        return
    if args.action == "restore":
        if args.bundle is None or args.target is None:
            parser.error("restore requires --bundle and --target (a new database path)")
        print(json.dumps(restore(args.bundle, args.target)))
        return
    if args.action == "permissions":
        print(json.dumps(permissions(path)))
        return
    if args.action == "report":
        print(json.dumps(report(path)))
        return
    print(json.dumps(check_database(path, read_only=args.action == "check")))
    if args.action == "serve":
        # Connections close after initialization. Future writers must be serialized;
        # this process does not hold a database lock or pretend to be a SQL server.
        stopped = Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stopped.set())
        if backup_directory:
            interval = int(os.environ.get("RETAILFLOW_DUCKDB_BACKUP_SECONDS", "86400"))
            if interval < 60:
                raise ValueError("Backup interval must be at least 60 seconds")
            while not stopped.is_set():
                try:
                    result = backup(path, Path(backup_directory))
                    print(json.dumps({"event": "warehouse_backup_completed", **result}))
                    delay = interval
                except Exception as error:
                    print(
                        json.dumps(
                            {"event": "warehouse_backup_failed", "error_type": type(error).__name__}
                        )
                    )
                    delay = 60
                stopped.wait(delay)
        else:
            stopped.wait()
        print(json.dumps({"event": "warehouse_runtime_stopped"}))


if __name__ == "__main__":
    main()
