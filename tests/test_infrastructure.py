import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from retailflow import infrastructure


def test_orchestration_secret_initialization_is_non_destructive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(infrastructure, "ROOT", tmp_path)
    for key in (
        "AIRFLOW_DB_PASSWORD",
        "AIRFLOW_POSTGRES_ADMIN_PASSWORD",
        "RETAILFLOW_AIRFLOW_PASSWORD",
        "AIRFLOW_FERNET_KEY",
        "AIRFLOW_JWT_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text("RETAILFLOW_INTERVAL_SECONDS=600\n", encoding="utf-8")
    infrastructure.initialize_orchestration()
    first = path.read_bytes()
    infrastructure.initialize_orchestration()
    assert path.read_bytes() == first
    assert path.read_text(encoding="utf-8").splitlines()[0] == "RETAILFLOW_INTERVAL_SECONDS=600"


def test_container_replacement_is_reported_separately_from_data_deletion() -> None:
    summary = infrastructure.plan_summary(
        {
            "resource_changes": [
                {
                    "address": "docker_container.azurite",
                    "change": {"importing": {"id": "local-id"}, "actions": ["delete", "create"]},
                },
                {
                    "address": "docker_volume.azurite",
                    "change": {"importing": {"id": "volume"}, "actions": ["no-op"]},
                },
            ]
        }
    )
    assert summary["replacements"] == ["docker_container.azurite"]
    assert summary["imports"] == ["docker_container.azurite", "docker_volume.azurite"]
    assert summary["deletes"] == []


@pytest.mark.parametrize(
    "address,actions",
    [
        ("docker_volume.azurite", ["delete", "create"]),
        ("docker_network.azurite", ["delete"]),
        ("docker_container.azurite", ["delete"]),
        ("azurerm_storage_account.unexpected", ["create"]),
        ("docker_volume.duckdb", ["delete", "create"]),
        ("docker_volume.duckdb_backups", ["delete", "create"]),
        ("docker_volume.mailpit", ["delete", "create"]),
        ("docker_network.mailpit", ["delete", "create"]),
        ("docker_volume.dbt_artifacts", ["delete", "create"]),
        ("docker_volume.duckdb", ["forget"]),
        ("docker_volume.airflow_postgres", ["delete", "create"]),
        ("docker_volume.airflow_state", ["delete"]),
        ("docker_volume.airflow_logs", ["delete"]),
    ],
)
def test_unsafe_plans_are_rejected(address: str, actions: list[str]) -> None:
    with pytest.raises(ValueError):
        infrastructure.plan_summary(
            {"resource_changes": [{"address": address, "change": {"actions": actions}}]}
        )


def test_duckdb_runtime_resources_are_allowed() -> None:
    resources = ["docker_image.duckdb", "docker_volume.duckdb", "docker_container.duckdb"]
    summary = infrastructure.plan_summary(
        {
            "resource_changes": [
                {"address": address, "change": {"actions": ["create"]}} for address in resources
            ]
        }
    )
    assert summary["creates"] == resources
    assert summary["replacements"] == []


def test_local_mail_resources_are_allowed_without_loosening_deletion_guards():
    resources = [
        "docker_image.mailpit",
        "docker_network.mailpit",
        "docker_volume.mailpit",
        "docker_container.mailpit",
    ]
    summary = infrastructure.plan_summary(
        {
            "resource_changes": [
                {"address": address, "change": {"actions": ["create"]}} for address in resources
            ]
        }
    )
    assert summary["creates"] == resources and not summary["replacements"]


def test_backup_verification_detects_source_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blobs = {"orders/batch/data.csv": b"header\n1\n", "fx/data.json": b"[]"}

    class FakeStore:
        def __init__(self) -> None:
            self.container = SimpleNamespace(
                list_blobs=lambda: [SimpleNamespace(name=name) for name in blobs]
            )

        def read(self, name: str) -> bytes:
            return blobs[name]

        def close(self) -> None:
            pass

    monkeypatch.setattr(infrastructure, "BACKUP", tmp_path)
    monkeypatch.setattr(infrastructure, "BlobStore", FakeStore)
    infrastructure.snapshot()
    assert infrastructure.verify_snapshot() == 2
    blobs["fx/data.json"] = b"changed"
    with pytest.raises(ValueError, match="verification failed"):
        infrastructure.verify_snapshot()


def test_isolated_review_plan_preserves_legacy_plan(monkeypatch, tmp_path):
    directory = tmp_path / "infra/local"
    directory.mkdir(parents=True)
    legacy = directory / "migration.tfplan"
    legacy.write_bytes(b"existing saved plan")
    review = tmp_path / "artifacts/release-reviews/attempt"
    monkeypatch.setattr(infrastructure, "ROOT", tmp_path)
    monkeypatch.setattr(infrastructure, "DIRECTORY", directory)
    monkeypatch.setattr(infrastructure, "BACKUP", tmp_path / "legacy-backup")
    monkeypatch.chdir(tmp_path)
    for name in (
        "AZURITE_ACCOUNT_KEY",
        "AIRFLOW_DB_PASSWORD",
        "AIRFLOW_POSTGRES_ADMIN_PASSWORD",
        "RETAILFLOW_AIRFLOW_PASSWORD",
        "AIRFLOW_FERNET_KEY",
        "AIRFLOW_JWT_SECRET",
    ):
        monkeypatch.setenv(name, "synthetic-value")
    monkeypatch.setattr(
        infrastructure,
        "prepare_imports",
        lambda env: (directory / "imports.local.tf").write_text("docker_container.azurite"),
    )
    backups = []
    monkeypatch.setattr(infrastructure, "snapshot", lambda: backups.append(infrastructure.BACKUP))
    calls = []

    def terraform(env, *args):
        calls.append(args)
        if args[0] == "plan":
            Path(args[-1].removeprefix("-out=")).write_bytes(b"new saved plan")
        return json.dumps({"resource_changes": []}) if args[0] == "show" else ""

    monkeypatch.setattr(infrastructure, "terraform", terraform)
    monkeypatch.setattr(sys, "argv", ["infrastructure", "plan", "--review-directory", str(review)])
    infrastructure.main()
    assert legacy.read_bytes() == b"existing saved plan"
    assert (review / "review.tfplan").read_bytes() == b"new saved plan"
    assert backups == [review / "blobs"]
    assert not any(call[0] == "apply" for call in calls)
    with pytest.raises(SystemExit):
        infrastructure.main()


def test_review_directory_must_stay_in_ignored_artifacts(monkeypatch, tmp_path):
    monkeypatch.setattr(infrastructure, "ROOT", tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["infrastructure", "plan", "--review-directory", str(tmp_path)]
    )
    with pytest.raises(SystemExit):
        infrastructure.main()
