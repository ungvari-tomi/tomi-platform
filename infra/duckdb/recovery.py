"""Checkpointed local snapshots and non-overwriting recovery drills."""

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import duckdb
from filelock import FileLock


def fingerprint(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def inventory(path: Path) -> dict:
    with duckdb.connect(
        str(path), read_only=True, config={"threads": 1, "memory_limit": "256MB"}
    ) as connection:
        tables = connection.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_catalog=CURRENT_DATABASE() AND table_type='BASE TABLE' "
            "ORDER BY table_schema, table_name"
        ).fetchall()
        counts = []
        for schema, table in tables:
            # Names come from the catalog and are quoted as SQL identifiers.
            quoted = ".".join('"' + name.replace('"', '""') + '"' for name in (schema, table))
            count = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]  # nosec B608
            counts.append({"schema": schema, "table": table, "rows": count})
        return {
            "duckdb_version": connection.execute("SELECT VERSION()").fetchone()[0],
            "tables": counts,
        }


def durable_copy(source: Path, destination: Path) -> None:
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def backup(database: Path, directory: Path, *, timeout: float = 60) -> dict:
    database = database.resolve()
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    identity = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex
    pending = directory / (".pending-" + identity)
    complete = directory / identity
    pending.mkdir(mode=0o700)
    try:
        with FileLock(str(database) + ".lock", mode=0o660, timeout=timeout):
            if not database.is_file():
                raise FileNotFoundError(database)
            with duckdb.connect(
                str(database), config={"threads": 1, "memory_limit": "256MB"}
            ) as connection:
                connection.execute("CHECKPOINT")
            # Connection is closed, WAL is checkpointed, cooperating writers excluded.
            payload = pending / "warehouse.duckdb"
            durable_copy(database, payload)
            manifest = {
                "version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                **fingerprint(payload),
                **inventory(payload),
            }
        with (pending / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        pending.rename(complete)
        return {"backup": str(complete), **manifest}
    finally:
        # Only this call's newly allocated staging directory can be removed.
        if pending.exists():
            if pending.resolve().parent != directory or pending.is_symlink():
                raise ValueError("Refusing cleanup outside the allocated backup directory")
            shutil.rmtree(pending)


def restore(bundle: Path, target: Path, *, timeout: float = 60) -> dict:
    """Restore to a new path only; never replace the active warehouse."""
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != 1:
        raise ValueError("Unsupported backup version")
    source = bundle / "warehouse.duckdb"
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(target) + ".lock", mode=0o660, timeout=timeout):
        if target.exists() or Path(str(target) + ".wal").exists():
            raise FileExistsError("Recovery requires a new target without a WAL")
        created = False
        try:
            # Exclusive creation protects an existing target even from a noncooperating caller.
            with source.open("rb") as incoming, target.open("xb") as outgoing:
                created = True
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if fingerprint(target) != {key: manifest[key] for key in ("sha256", "bytes")}:
                raise ValueError("Backup checksum or length mismatch")
            observed = inventory(target)
            if observed["tables"] != manifest["tables"]:
                raise ValueError("Recovered table inventory differs from backup")
            return {"target": str(target), "status": "verified", **observed}
        except BaseException:
            if created:
                target.unlink(missing_ok=True)
            raise
