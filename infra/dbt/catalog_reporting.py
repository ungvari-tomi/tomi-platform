"""Verified catalog-aware aggregate snapshots, separate from the sales pointer."""

import json
import os
import re
import runpy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import duckdb
from filelock import FileLock

# The same immutable, baked-in reader/publisher helpers in all three runtimes.
base = SimpleNamespace(**runpy.run_path(str(Path(__file__).with_name("reporting.py"))))
SALES = list(
    zip(
        base.COLUMNS,
        [
            "VARCHAR",
            "DATE",
            "VARCHAR",
            "VARCHAR",
            "VARCHAR",
            "BIGINT",
            "BIGINT",
            "BIGINT",
            "BIGINT",
            "HUGEINT",
            "TIMESTAMPTZ",
        ],
        strict=True,
    )
)
DETAILS = [("product_name", "VARCHAR"), ("category", "VARCHAR"), ("catalog_status", "VARCHAR")]
TABLES = {
    "catalog_sales": SALES + DETAILS,
    "inventory_movements": [
        ("seed", "BIGINT"),
        ("interval_seconds", "INTEGER"),
        ("product_id", "VARCHAR"),
        ("warehouse_id", "VARCHAR"),
        *DETAILS,
        ("movement_count", "BIGINT"),
        ("net_quantity_moved", "HUGEINT"),
        ("inbound_units", "HUGEINT"),
        ("outbound_units", "HUGEINT"),
        ("latest_movement_at", "TIMESTAMPTZ"),
    ],
    "products": [
        ("product_id", "VARCHAR"),
        ("sku", "VARCHAR"),
        *DETAILS,
        ("revision", "INTEGER"),
        ("updated_at", "TIMESTAMPTZ"),
        ("catalog_observed_at", "TIMESTAMPTZ"),
    ],
}
QUERIES = {
    "catalog_sales": """SELECT s.*, p.product_name, p.category, p.catalog_status
        FROM staging.daily_sales s LEFT JOIN staging.current_products p
        ON s.product_id = p.product_id""",
    "inventory_movements": """SELECT seed, interval_seconds, product_id, warehouse_id,
        current_product_name AS product_name, current_product_category AS category, catalog_status,
        COUNT(*)::BIGINT AS movement_count, SUM(quantity_delta)::HUGEINT AS net_quantity_moved,
        SUM(GREATEST(quantity_delta, 0))::HUGEINT AS inbound_units,
        SUM(GREATEST(-quantity_delta, 0))::HUGEINT AS outbound_units,
        MAX(occurred_at) AS latest_movement_at
        FROM staging.catalog_inventory
        GROUP BY seed, interval_seconds, product_id, warehouse_id,
                 current_product_name, current_product_category, catalog_status""",
    "products": """SELECT product_id, sku, product_name, category, catalog_status,
        revision, updated_at, catalog_observed_at FROM staging.current_products""",
}


def validate(db) -> dict:
    objects = db.execute("""SELECT table_schema,table_name,table_type FROM information_schema.tables
        WHERE table_schema NOT IN ('information_schema','pg_catalog')
        ORDER BY table_name""").fetchall()
    if objects != [("main", name, "BASE TABLE") for name in sorted(TABLES)]:
        raise ValueError("Unexpected catalog reporting objects")
    counts = {}
    for name, fields in TABLES.items():
        actual = [(r[0], r[1]) for r in db.execute(f"DESCRIBE {name}").fetchall()]
        expected = [(n, "TIMESTAMP WITH TIME ZONE" if t == "TIMESTAMPTZ" else t) for n, t in fields]
        if actual != expected:
            raise ValueError("Catalog reporting schema mismatch")
        # Identifiers come only from TABLES, never caller input or manifest content.
        counts[name] = db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]  # nosec B608
        if not 0 <= counts[name] <= base.MAX_ROWS:
            raise ValueError("Catalog reporting row limit exceeded")
    base.validate_report(db, "catalog_sales")
    invalid = db.execute("""SELECT COUNT(*) FROM inventory_movements
        WHERE seed IS NULL OR interval_seconds IS NULL OR interval_seconds <= 0
        OR product_id IS NULL OR warehouse_id IS NULL
        OR movement_count IS NULL OR movement_count <= 0
        OR net_quantity_moved IS NULL OR inbound_units IS NULL OR outbound_units IS NULL
        OR inbound_units < 0 OR outbound_units < 0 OR latest_movement_at IS NULL
        OR net_quantity_moved <> inbound_units - outbound_units""").fetchone()[0]
    duplicate = db.execute("""SELECT COUNT(*) FROM (
        SELECT seed,interval_seconds,product_id,warehouse_id FROM inventory_movements
        GROUP BY ALL HAVING COUNT(*) > 1)""").fetchone()[0]
    products = db.execute("""SELECT COUNT(*),COUNT(DISTINCT product_id),COUNT(DISTINCT sku)
        FROM products""").fetchone()
    invalid_products = db.execute("""SELECT COUNT(*) FROM products
        WHERE product_id IS NULL OR sku IS NULL OR product_name IS NULL
        OR LENGTH(product_name) NOT BETWEEN 1 AND 120
        OR category IS NULL OR category NOT IN ('electronics','apparel','homeware')
        OR catalog_status IS NULL OR catalog_status NOT IN ('active','retired')
        OR revision IS NULL OR revision < 1
        OR updated_at IS NULL OR catalog_observed_at IS NULL""").fetchone()[0]
    for name in ("catalog_sales", "inventory_movements"):
        invalid += db.execute(f"""SELECT COUNT(*) FROM {name} a LEFT JOIN products p
            ON a.product_id=p.product_id WHERE
            a.product_name IS DISTINCT FROM p.product_name
            OR a.category IS DISTINCT FROM p.category
            OR a.catalog_status IS DISTINCT FROM p.catalog_status""").fetchone()[0]  # nosec B608
    if invalid or duplicate or products != (20, 20, 20) or invalid_products:
        raise ValueError("Catalog aggregate contract failed")
    return counts


def publish(database: Path, root: Path, *, warehouse_lock: FileLock | None = None) -> dict:
    database, root = database.resolve(), root.resolve()
    if not database.is_file() or root == database.parent or database.is_relative_to(root):
        raise ValueError("Catalog reporting requires separate protected storage")
    lock = warehouse_lock or FileLock(str(database) + ".lock", mode=0o660, timeout=60)
    if Path(lock.lock_file).resolve() != Path(str(database) + ".lock"):
        raise ValueError("Unexpected warehouse lock")
    root.mkdir(parents=True, exist_ok=True, mode=0o2770)
    with lock, FileLock(root / ".publish.lock", mode=0o660, timeout=60):
        version = uuid4().hex
        pending = root / (".pending-" + version)
        pending.mkdir(mode=0o2770)
        target = pending / "retailflow.duckdb"
        with duckdb.connect(str(database), read_only=True, config=base.SETTINGS) as source:
            with duckdb.connect(str(target), config=base.SETTINGS) as destination:
                destination.execute("BEGIN TRANSACTION")
                for name, fields in TABLES.items():
                    query = source.execute(QUERIES[name])
                    if tuple(c[0] for c in query.description) != tuple(f[0] for f in fields):
                        raise ValueError("Unexpected catalog source columns")
                    destination.execute(
                        f"CREATE TABLE {name} ("
                        + ",".join(f"{column} {kind}" for column, kind in fields)
                        + ")"
                    )
                    count = 0
                    while rows := query.fetchmany(1000):
                        count += len(rows)
                        if count > base.MAX_ROWS:
                            raise ValueError("Catalog snapshot row limit exceeded")
                        destination.executemany(
                            f"INSERT INTO {name} VALUES (" + ",".join("?" for _ in fields) + ")",  # nosec B608
                            rows,
                        )
                counts = validate(destination)
                destination.execute("COMMIT")
                destination.execute("CHECKPOINT")
            observed = source.execute("""SELECT MAX(retrieved_at) FROM ops.loaded_batches
                WHERE source='product-catalog/products-v1'""").fetchone()[0]
        if observed is None:
            raise ValueError("Missing verified catalog observation")
        target.chmod(0o640)
        if target.stat().st_size > base.MAX_BYTES:
            raise ValueError("Catalog snapshot byte limit exceeded")
        with target.open("rb+") as payload:
            os.fsync(payload.fileno())
        with duckdb.connect(str(target), read_only=True, config=base.SETTINGS) as verified:
            if validate(verified) != counts:
                raise ValueError("Closed catalog snapshot differs")
            latest_order = verified.execute(
                "SELECT MAX(latest_order_at) FROM catalog_sales"
            ).fetchone()[0]
            latest_inventory = verified.execute(
                "SELECT MAX(latest_movement_at) FROM inventory_movements"
            ).fetchone()[0]
        manifest = {
            "format_version": 1,
            "kind": "catalog-v1",
            "version": version,
            "published_at": datetime.now(UTC).isoformat(),
            "rows": counts,
            "bytes": target.stat().st_size,
            "sha256": base.checksum(target),
            "catalog_retrieved_at": observed.isoformat(),
            "latest_order_at": latest_order.isoformat() if latest_order else None,
            "latest_inventory_at": latest_inventory.isoformat() if latest_inventory else None,
        }
        base.write_json(pending / "manifest.json", manifest)
        os.replace(pending, root / version)
        pointer = root / (".current-" + version + ".json")
        base.write_json(pointer, {"format_version": 1, "version": version})
        os.replace(pointer, root / "current.json")
        return manifest


def load_snapshot(root: Path, version: str) -> tuple[dict, dict]:
    if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{32}", version):
        raise ValueError("Invalid catalog reporting version")
    root = root.resolve()
    directory = root / version
    manifest_path, database = directory / "manifest.json", directory / "retailflow.duckdb"
    if directory.resolve() != directory or manifest_path.is_symlink() or database.is_symlink():
        raise ValueError("Unsafe catalog reporting path")
    if manifest_path.stat().st_size > 4096:
        raise ValueError("Catalog manifest exceeds size limit")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    keys = {
        "format_version",
        "kind",
        "version",
        "published_at",
        "rows",
        "bytes",
        "sha256",
        "catalog_retrieved_at",
        "latest_order_at",
        "latest_inventory_at",
    }
    if (
        set(manifest) != keys
        or type(manifest["format_version"]) is not int
        or manifest["format_version"] != 1
        or manifest["kind"] != "catalog-v1"
        or manifest["version"] != version
        or type(manifest["bytes"]) is not int
        or not 0 < manifest["bytes"] <= base.MAX_BYTES
        or database.stat().st_size != manifest["bytes"]
        or base.checksum(database) != manifest["sha256"]
        or not isinstance(manifest["rows"], dict)
        or set(manifest["rows"]) != set(TABLES)
        or any(type(v) is not int or not 0 <= v <= base.MAX_ROWS for v in manifest["rows"].values())
    ):
        raise ValueError("Catalog checksum or manifest contract failed")
    for field in ("published_at", "catalog_retrieved_at", "latest_order_at", "latest_inventory_at"):
        if field in ("published_at", "catalog_retrieved_at") and not isinstance(
            manifest[field], str
        ):
            raise ValueError("Required catalog observation time missing")
        if (
            manifest[field] is not None
            and datetime.fromisoformat(manifest[field]).utcoffset() is None
        ):
            raise ValueError("Catalog snapshot timestamps require timezones")
    with duckdb.connect(str(database), read_only=True, config=base.SETTINGS) as db:
        if validate(db) != manifest["rows"]:
            raise ValueError("Catalog reporting row count mismatch")
        rows = {
            name: [
                dict(zip((f[0] for f in fields), row, strict=True))
                for row in db.execute(f"SELECT * FROM {name}").fetchall()  # nosec B608
            ]
            for name, fields in TABLES.items()
        }
    return manifest, rows
