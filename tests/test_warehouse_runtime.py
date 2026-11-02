import tomllib
from pathlib import Path

import pytest

from infra.duckdb.runner import check_database


def test_runtime_creates_and_reopens_empty_database(tmp_path: Path) -> None:
    path = tmp_path / "warehouse.duckdb"
    assert check_database(path)["status"] == "ok"
    assert path.is_file()
    assert check_database(path, read_only=True)["status"] == "ok"
    import duckdb

    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SHOW TABLES").fetchall() == []


def test_health_check_does_not_create_missing_database(tmp_path: Path) -> None:
    import duckdb

    path = tmp_path / "missing.duckdb"
    with pytest.raises(duckdb.IOException):
        check_database(path, read_only=True)
    assert not path.exists()


def test_container_dependency_matches_project_lock() -> None:
    root = Path(__file__).resolve().parents[1]
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    requirements = (root / "infra/duckdb/requirements.txt").read_text(encoding="utf-8")
    package = None
    for line in requirements.splitlines():
        if "==" in line and not line.startswith("#"):
            name, version = line.strip().split()[0].split("==")
            package = next(item for item in lock["package"] if item["name"] == name)
            assert package["version"] == version
        if "--hash=" in line:
            digest = line.split("--hash=", 1)[1].strip().rstrip("\\").strip()
            assert package and any(wheel["hash"] == digest for wheel in package["wheels"])
