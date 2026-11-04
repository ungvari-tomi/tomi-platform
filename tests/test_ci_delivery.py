import hashlib
import json
import zipfile
from types import SimpleNamespace

import pytest

from scripts import ci_safety, release_bundle


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "infra/local/terraform.tfstate",
        "infra/local/review.tfplan",
        "infra/local/imports.local.tf",
        "artifacts/test.xml",
        "data/orders.csv",
        "infra/local/.terraform/provider",
        "warehouse.duckdb.lock",
        "AGENTS.md",
        "docs/agents.md",
        ".local-secrets/customer.json",
    ],
)
def test_operational_files_are_rejected_without_reading_them(monkeypatch, tmp_path, name):
    monkeypatch.setattr(
        ci_safety.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=(name + "\0").encode())
    )
    with pytest.raises(ValueError, match="Operational/private"):
        ci_safety.source_files(tmp_path)


def test_example_config_and_provider_lock_are_allowed(monkeypatch, tmp_path):
    names = [".env.example", "infra/local/.terraform.lock.hcl"]
    monkeypatch.setattr(
        ci_safety.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout=("\0".join(names) + "\0").encode()),
    )
    assert ci_safety.source_files(tmp_path) == sorted(names)


def test_secret_failure_report_does_not_print_the_secret(tmp_path):
    path = tmp_path / "example.py"
    path.write_text("-----BEGIN " + "RSA PRIVATE KEY-----\nsynthetic test\n", encoding="utf-8")
    findings = ci_safety.scan(tmp_path, ["example.py"])
    assert findings and all(set(f) == {"file", "line", "type"} for f in findings)
    assert "BEGIN" not in json.dumps(findings)


def test_delivery_package_is_complete_reproducible_and_never_overwrites(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    names = [
        "pyproject.toml",
        "uv.lock",
        "infra/local/main.tf",
        "infra/dbt/uv.lock",
        "infra/orchestration/airflow.Dockerfile",
        "dags/retailflow_extraction.py",
    ]
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source:" + name)
    (root / ".env").write_text("private configuration, not tracked")
    monkeypatch.setattr(
        release_bundle, "git", lambda root, *args: "" if args[0] == "status" else "a" * 40
    )
    monkeypatch.setattr(release_bundle, "source_files", lambda root: names)
    archive = release_bundle.package(root, tmp_path / "one")
    with zipfile.ZipFile(archive) as bundle:
        assert "source/.env" not in bundle.namelist()
        manifest = json.loads(bundle.read("release.json"))
        assert manifest["git_commit"] == "a" * 40
        for name, digest in manifest["files"].items():
            assert hashlib.sha256(bundle.read("source/" + name)).hexdigest() == digest
    other = release_bundle.package(root, tmp_path / "two")
    assert archive.read_bytes() == other.read_bytes()
    assert (
        archive.with_name(archive.name + ".sha256").read_text().split()[0]
        == hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    with pytest.raises(FileExistsError):
        release_bundle.package(root, tmp_path / "one")


def test_delivery_refuses_uncommitted_worktree(monkeypatch, tmp_path):
    monkeypatch.setattr(release_bundle, "git", lambda *a: "?? changed.py")
    with pytest.raises(ValueError, match="clean committed"):
        release_bundle.package(tmp_path, tmp_path / "out")
    assert not (tmp_path / "out").exists()
