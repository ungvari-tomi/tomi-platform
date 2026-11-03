import hashlib
import json
import zipfile
from types import SimpleNamespace

import pytest

from scripts import release_bundle, release_review

COMMIT = "a" * 40


@pytest.fixture
def delivery(monkeypatch, tmp_path):
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
    contents = {name: ("source:" + name).encode() for name in names}
    for name, content in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    monkeypatch.setattr(release_bundle, "source_files", lambda root: names)
    monkeypatch.setattr(
        release_bundle, "git", lambda root, *args: "" if args[0] == "status" else COMMIT
    )
    archive = release_bundle.package(root, tmp_path / "delivery")
    monkeypatch.setattr(release_review, "source_files", lambda root: names)

    def git(root, *args):
        if args[0] == "status":
            return b""
        if args[0] == "rev-parse":
            return COMMIT.encode()
        return contents[args[1].split(":", 1)[1]]

    monkeypatch.setattr(release_review, "git", git)
    return root, archive


def rewrite(archive, change):
    with zipfile.ZipFile(archive) as bundle:
        entries = {name: bundle.read(name) for name in bundle.namelist()}
    change(entries)
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, content in entries.items():
            bundle.writestr(name, content)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n")


def test_verify_source_delivery_without_extracting(delivery):
    root, archive = delivery
    report = release_review.verify_delivery(root, archive, COMMIT)
    assert report["source_files_verified"] == 6
    assert report["commit"] == COMMIT
    assert not (root / "artifacts").exists()


@pytest.mark.parametrize("commit", ["main", "a" * 7, "b" * 40])
def test_rejects_wrong_commit(delivery, commit):
    with pytest.raises(ValueError):
        release_review.verify_delivery(*delivery, commit)


def test_rejects_corrupt_zip_before_reading_manifest(delivery):
    root, archive = delivery
    archive.write_bytes(archive.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="ZIP checksum"):
        release_review.verify_delivery(root, archive, COMMIT)


@pytest.mark.parametrize("extra", ["source/.env", "../../outside", "source/data/orders.csv"])
def test_rejects_unexpected_archive_entries(delivery, extra):
    root, archive = delivery
    rewrite(archive, lambda entries: entries.update({extra: b"unexpected"}))
    with pytest.raises(ValueError, match="delivery entries"):
        release_review.verify_delivery(root, archive, COMMIT)


def test_rejects_rehashed_source_that_differs_from_git(delivery):
    root, archive = delivery

    def tamper(entries):
        entries["source/uv.lock"] = b"different source"
        manifest = json.loads(entries["release.json"])
        manifest["files"]["uv.lock"] = hashlib.sha256(b"different source").hexdigest()
        entries["release.json"] = json.dumps(manifest).encode()

    rewrite(archive, tamper)
    with pytest.raises(ValueError, match="committed source"):
        release_review.verify_delivery(root, archive, COMMIT)


def test_dirty_worktree_fails_before_infrastructure(delivery, monkeypatch):
    root, archive = delivery
    monkeypatch.setattr(release_review, "git", lambda *args: b" M changed.py")
    with pytest.raises(ValueError, match="clean committed"):
        release_review.prepare_review(root, archive, COMMIT)
    assert not (root / "artifacts").exists()


def test_review_preserves_separate_attempts_and_never_applies(delivery, monkeypatch):
    root, archive = delivery
    calls = []

    def infrastructure(root, action, review):
        calls.append(action)
        if action == "plan":
            (review / "review.tfplan").write_bytes(b"opaque saved plan")
            (review / "plan-summary.json").write_text(json.dumps({"creates": []}))
        if action == "verify":
            return {"verified_blobs": 4}

    monkeypatch.setattr(release_review, "infrastructure", infrastructure)
    first = release_review.prepare_review(root, archive, COMMIT)
    second = release_review.prepare_review(root, archive, COMMIT)
    assert first != second and first.exists() and second.exists()
    assert calls == ["validate", "plan", "verify"] * 2
    report = json.loads(first.read_text())
    assert report["verified_blobs"] == 4
    assert report["apply_performed"] is False
    assert report["requires_changes"] is False
    assert report["plan_sha256"] == hashlib.sha256(b"opaque saved plan").hexdigest()


@pytest.mark.parametrize("failure_stage", ["plan", "verify"])
def test_failed_plan_or_backup_has_no_success_report(delivery, monkeypatch, failure_stage):
    root, archive = delivery

    def infrastructure(root, action, review):
        if action == failure_stage:
            raise RuntimeError("simulated failure")

    monkeypatch.setattr(release_review, "infrastructure", infrastructure)
    with pytest.raises(RuntimeError, match="simulated"):
        release_review.prepare_review(root, archive, COMMIT)
    assert not list((root / "artifacts").rglob("review.json"))


def test_helper_failure_does_not_expose_captured_output(tmp_path, monkeypatch):
    monkeypatch.setattr(
        release_review.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1, stdout="private-value", stderr="private-value"
        ),
    )
    with pytest.raises(RuntimeError) as error:
        release_review.infrastructure(tmp_path, "plan", tmp_path)
    assert "private-value" not in str(error.value)
