"""Recovery restores complete snapshots, never a partial or overwritten database."""

import json
from pathlib import Path

import duckdb
import pytest
from filelock import FileLock, Timeout

from infra.duckdb.recovery import backup, inventory, restore


def warehouse(path: Path) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE SCHEMA raw; CREATE SCHEMA ops")
        connection.execute("CREATE TABLE raw.orders(id BIGINT PRIMARY KEY, amount DECIMAL(24,12))")
        connection.execute("CREATE TABLE ops.loaded_batches(manifest VARCHAR PRIMARY KEY)")
        connection.execute("BEGIN")
        connection.execute("INSERT INTO raw.orders VALUES (1, 366.330000000001)")
        connection.execute("INSERT INTO ops.loaded_batches VALUES ('batch-one')")
        connection.execute("COMMIT")


def test_snapshot_restores_data_and_ledger_independently_of_later_writes(tmp_path: Path) -> None:
    source = tmp_path / "live.duckdb"
    warehouse(source)
    result = backup(source, tmp_path / "backups")
    with duckdb.connect(str(source)) as connection:
        connection.execute("DELETE FROM raw.orders; DELETE FROM ops.loaded_batches")
    target = tmp_path / "restored.duckdb"
    assert restore(Path(result["backup"]), target)["status"] == "verified"
    with duckdb.connect(str(target), read_only=True) as connection:
        assert (
            str(connection.execute("SELECT amount FROM raw.orders").fetchone()[0])
            == "366.330000000001"
        )
        assert connection.execute("SELECT * FROM ops.loaded_batches").fetchall() == [("batch-one",)]
    assert inventory(target)["tables"] == result["tables"]


def test_corrupt_snapshot_is_rejected_and_partial_target_removed(tmp_path: Path) -> None:
    source = tmp_path / "live.duckdb"
    warehouse(source)
    bundle = Path(backup(source, tmp_path / "backups")["backup"])
    with (bundle / "warehouse.duckdb").open("ab") as stream:
        stream.write(b"corruption")
    target = tmp_path / "restore.duckdb"
    with pytest.raises(ValueError, match="checksum"):
        restore(bundle, target)
    assert not target.exists()


@pytest.mark.parametrize("existing_suffix", ["", ".wal"])
def test_restore_refuses_existing_database_or_wal(tmp_path: Path, existing_suffix: str) -> None:
    source = tmp_path / "live.duckdb"
    warehouse(source)
    bundle = Path(backup(source, tmp_path / "backups")["backup"])
    target = tmp_path / "restore.duckdb"
    existing = Path(str(target) + existing_suffix)
    existing.write_bytes(b"preserve me")
    with pytest.raises(FileExistsError):
        restore(bundle, target)
    assert existing.read_bytes() == b"preserve me"


def test_backup_obeys_writer_lock_and_does_not_publish_incomplete_bundle(tmp_path: Path) -> None:
    source = tmp_path / "live.duckdb"
    warehouse(source)
    directory = tmp_path / "backups"
    with FileLock(str(source) + ".lock", mode=0o660):
        with pytest.raises(Timeout):
            backup(source, directory, timeout=0)
    assert list(directory.iterdir()) == []
    assert backup(source, directory)["tables"]


def test_inventory_mismatch_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "live.duckdb"
    warehouse(source)
    bundle = Path(backup(source, tmp_path / "backups")["backup"])
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["tables"][0]["rows"] = 999
    manifest_path.write_text(json.dumps(manifest))
    target = tmp_path / "restore.duckdb"
    with pytest.raises(ValueError, match="inventory"):
        restore(bundle, target)
    assert not target.exists()


def test_missing_source_does_not_create_database_or_publish_backup(tmp_path: Path) -> None:
    source = tmp_path / "missing.duckdb"
    directory = tmp_path / "backups"
    with pytest.raises(FileNotFoundError):
        backup(source, directory)
    assert not source.exists()
    assert list(directory.iterdir()) == []
