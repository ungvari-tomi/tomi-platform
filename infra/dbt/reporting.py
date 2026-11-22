"""Publish and read immutable aggregate-only reporting databases."""

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import duckdb
from filelock import FileLock

COLUMNS = (
    "dataset_id",
    "order_date",
    "country_code",
    "product_id",
    "currency_code",
    "order_count",
    "units_sold",
    "fx_covered_orders",
    "fx_uncovered_orders",
    "revenue_eur_minor",
    "latest_order_at",
)
CREATE_REPORT = """CREATE TABLE daily_sales (
    dataset_id VARCHAR, order_date DATE, country_code VARCHAR, product_id VARCHAR,
    currency_code VARCHAR, order_count BIGINT, units_sold BIGINT,
    fx_covered_orders BIGINT, fx_uncovered_orders BIGINT, revenue_eur_minor HUGEINT,
    latest_order_at TIMESTAMPTZ)"""
MAX_ROWS = 100_000
MAX_BYTES = 64 * 1024 * 1024
SETTINGS = {
    "threads": 1,
    "memory_limit": "128MB",
    "enable_external_access": False,
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
}


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    path.chmod(0o640)


def validate_report(db, table: str = "daily_sales") -> None:
    if table not in {"daily_sales", "catalog_sales"}:
        raise ValueError("Unsupported aggregate table")
    invalid = db.execute(
        """SELECT COUNT(*) FROM daily_sales
        WHERE dataset_id IS NULL OR order_date IS NULL OR country_code IS NULL
           OR product_id IS NULL OR currency_code IS NULL OR latest_order_at IS NULL
           OR order_count IS NULL OR order_count <= 0
           OR units_sold IS NULL OR units_sold < order_count
           OR fx_covered_orders IS NULL OR fx_uncovered_orders IS NULL
           OR fx_covered_orders < 0 OR fx_uncovered_orders < 0
           OR fx_covered_orders + fx_uncovered_orders != order_count
           OR (fx_covered_orders = 0 AND revenue_eur_minor IS NOT NULL)
           OR (fx_covered_orders > 0 AND (revenue_eur_minor IS NULL OR revenue_eur_minor <= 0))
           OR CAST(TIMEZONE('UTC', latest_order_at) AS DATE) != order_date""".replace(  # nosec B608
            "daily_sales", table
        )
    ).fetchone()[0]
    duplicate = db.execute(
        """SELECT COUNT(*) FROM (
        SELECT dataset_id, order_date, country_code, product_id, currency_code
        FROM daily_sales GROUP BY ALL HAVING COUNT(*) > 1)""".replace("daily_sales", table)  # nosec B608
    ).fetchone()[0]
    if invalid or duplicate:
        raise ValueError("Reporting aggregate contract failed")


def publish_report(database: Path, root: Path, *, warehouse_lock: FileLock | None = None) -> dict:
    """Reuse the wrapper's reentrant lock, keeping build/tests/publication serialized."""
    database = database.resolve()
    root = root.resolve()
    if not database.is_file() or root == database.parent or database.is_relative_to(root):
        raise ValueError("Reporting needs an existing warehouse and separate output storage")
    lock = warehouse_lock or FileLock(str(database) + ".lock", mode=0o660, timeout=60)
    if Path(lock.lock_file).resolve() != Path(str(database) + ".lock"):
        raise ValueError("Unexpected warehouse lock")
    root.mkdir(parents=True, exist_ok=True, mode=0o2770)
    with lock, FileLock(root / ".publish.lock", mode=0o660, timeout=60):
        version = uuid4().hex
        pending = root / (".pending-" + version)
        pending.mkdir(mode=0o2770)
        target = pending / "retailflow.duckdb"
        with duckdb.connect(str(database), read_only=True, config=SETTINGS) as source:
            query = source.execute("SELECT * FROM staging.daily_sales")
            if tuple(column[0] for column in query.description) != COLUMNS:
                raise ValueError("Unexpected reporting columns")
            rows = 0
            with duckdb.connect(str(target), config=SETTINGS) as destination:
                destination.execute(CREATE_REPORT)
                destination.execute("BEGIN TRANSACTION")
                while batch := query.fetchmany(1000):
                    rows += len(batch)
                    if rows > MAX_ROWS:
                        raise ValueError("Reporting snapshot row limit exceeded")
                    destination.executemany(
                        "INSERT INTO daily_sales VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch
                    )
                validate_report(destination)
                latest = destination.execute(
                    "SELECT MAX(latest_order_at) FROM daily_sales"
                ).fetchone()[0]
                destination.execute("COMMIT")
                destination.execute("CHECKPOINT")
        target.chmod(0o640)
        if target.stat().st_size > MAX_BYTES:
            raise ValueError("Reporting snapshot size limit exceeded")
        with target.open("rb+") as payload:
            os.fsync(payload.fileno())
        manifest = {
            "format_version": 1,
            "version": version,
            "published_at": datetime.now(UTC).isoformat(),
            "latest_order_at": latest.isoformat() if latest else None,
            "rows": rows,
            "bytes": target.stat().st_size,
            "sha256": checksum(target),
        }
        # Verify closed bytes and SQL before publishing either the version or pointer.
        with duckdb.connect(str(target), read_only=True, config=SETTINGS) as verified:
            validate_report(verified)
        write_json(pending / "manifest.json", manifest)
        os.replace(pending, root / version)
        pointer = root / (".current-" + version + ".json")
        write_json(pointer, {"format_version": 1, "version": version})
        os.replace(pointer, root / "current.json")
        return manifest


def current_version(root: Path) -> str:
    pointer = root / "current.json"
    if pointer.stat().st_size > 1024:
        raise ValueError("Invalid reporting pointer")
    record = json.loads(pointer.read_text(encoding="utf-8"))
    if set(record) != {"format_version", "version"} or record["format_version"] != 1:
        raise ValueError("Unsupported reporting pointer")
    version = record["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{32}", version):
        raise ValueError("Invalid reporting version")
    return version


def load_snapshot(root: Path, version: str) -> tuple[dict, list[dict]]:
    if not re.fullmatch(r"[a-f0-9]{32}", version):
        raise ValueError("Invalid reporting version")
    root = root.resolve()
    directory = root / version
    if directory.resolve() != directory or not directory.is_relative_to(root):
        raise ValueError("Reporting path escapes its volume")
    manifest_path = directory / "manifest.json"
    database = directory / "retailflow.duckdb"
    if manifest_path.is_symlink() or database.is_symlink() or manifest_path.stat().st_size > 4096:
        raise ValueError("Invalid reporting files")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        set(manifest)
        != {
            "format_version",
            "version",
            "published_at",
            "latest_order_at",
            "rows",
            "bytes",
            "sha256",
        }
        or manifest["format_version"] != 1
        or manifest["version"] != version
        or type(manifest["rows"]) is not int
        or not 0 <= manifest["rows"] <= MAX_ROWS
        or type(manifest["bytes"]) is not int
        or not 0 < manifest["bytes"] <= MAX_BYTES
        or database.stat().st_size != manifest["bytes"]
        or checksum(database) != manifest["sha256"]
    ):
        raise ValueError("Reporting checksum or manifest contract failed")
    published = datetime.fromisoformat(manifest["published_at"])
    if published.tzinfo is None:
        raise ValueError("Reporting publication time must have a timezone")
    with duckdb.connect(str(database), read_only=True, config=SETTINGS) as db:
        objects = db.execute("""SELECT table_schema, table_name FROM information_schema.tables
            WHERE table_schema NOT IN ('information_schema', 'pg_catalog')""").fetchall()
        if objects != [("main", "daily_sales")]:
            raise ValueError("Unexpected objects in reporting snapshot")
        query = db.execute("SELECT * FROM daily_sales")
        if tuple(c[0] for c in query.description) != COLUMNS:
            raise ValueError("Unexpected reporting columns")
        rows = query.fetchmany(MAX_ROWS + 1)
        if len(rows) != manifest["rows"]:
            raise ValueError("Reporting row count mismatch")
        validate_report(db)
    return manifest, [dict(zip(COLUMNS, row, strict=True)) for row in rows]
