"""Verify a delivery and prepare a local saved plan; never apply it."""

import argparse
import hashlib
import json
import re
import shutil

# Fixed local argument lists; no shell or credential output.
import subprocess  # nosec B404
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

from scripts.ci_safety import source_files

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *arguments: str) -> bytes:
    executable = shutil.which("git")
    if not executable:
        raise RuntimeError("Git executable is missing")
    result = subprocess.run(  # nosec B603
        [executable, *arguments], cwd=root, capture_output=True, timeout=60, check=False
    )
    if result.returncode:
        raise ValueError("Cannot read required local Git metadata")
    return result.stdout


def verify_delivery(root: Path, archive: Path, commit: str) -> dict:
    """Check the inner source ZIP without extracting or executing its contents."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected commit must be a full lowercase Git SHA")
    if git(root, "rev-parse", "HEAD").decode().strip() != commit:
        raise ValueError("Delivery commit must match HEAD in the existing managed checkout")
    expected_name = f"retailflow-{commit}.zip"
    if archive.name != expected_name or archive.stat().st_size > 50_000_000:
        raise ValueError("Unexpected delivery filename or size")
    checksum = archive.with_name(archive.name + ".sha256").read_text(encoding="utf-8").split()
    if len(checksum) != 2 or checksum[1] != expected_name:
        raise ValueError("Invalid delivery checksum file")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != checksum[0]:
        raise ValueError("Delivery ZIP checksum mismatch")
    expected_files = set(source_files(root))
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        expected_entries = {"release.json"} | {"source/" + name for name in expected_files}
        if (
            len(entries) != len(expected_entries)
            or {entry.filename for entry in entries} != expected_entries
            or sum(entry.file_size for entry in entries) > 50_000_000
        ):
            raise ValueError("Unexpected, duplicate or oversized delivery entries")
        manifest = json.loads(bundle.read("release.json"))
        if (
            manifest.get("format_version") != 1
            or manifest.get("git_commit") != commit
            or set(manifest.get("files", {})) != expected_files
        ):
            raise ValueError("Delivery manifest does not match expected source commit")
        for name in sorted(expected_files):
            content = bundle.read("source/" + name)
            if hashlib.sha256(content).hexdigest() != manifest["files"][name]:
                raise ValueError("Delivery source checksum mismatch")
            if content != git(root, "show", f"{commit}:{name}"):
                raise ValueError("Delivery content differs from committed source")
    return {
        "commit": commit,
        "archive_sha256": digest,
        "source_files_verified": len(expected_files),
    }


def infrastructure(root: Path, action: str, review: Path) -> dict | None:
    arguments = [sys.executable, "-m", "retailflow.infrastructure", action]
    if action != "validate":
        arguments += ["--review-directory", str(review)]
    result = subprocess.run(  # nosec B603
        arguments, cwd=root, capture_output=True, text=True, timeout=600, check=False
    )
    if result.returncode:
        # Raw Terraform/Docker output may contain sensitive configuration.
        raise RuntimeError(
            f"Infrastructure {action} failed; no successful release review published"
        )
    return json.loads(result.stdout) if action == "verify" else None


def prepare_review(root: Path, archive: Path, commit: str) -> Path:
    if git(root, "status", "--porcelain", "--untracked-files=all").strip():
        raise ValueError("Release review requires a clean committed worktree")
    verified = verify_delivery(root, archive, commit)
    base = root / "artifacts/release-reviews"
    base.mkdir(parents=True, exist_ok=True)
    # Serialize this command's plans/backups; do not run legacy helpers concurrently.
    with FileLock(base / ".review.lock", timeout=0):
        identity = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex
        review = base / identity
        review.mkdir(mode=0o700)
        infrastructure(root, "validate", review)
        print("Terraform validated; preparing blob backup and saved plan.", flush=True)
        infrastructure(root, "plan", review)
        blob_report = infrastructure(root, "verify", review)
        # Catch checkout changes during the long-running backup/plan operation.
        if git(root, "status", "--porcelain", "--untracked-files=all").strip():
            raise ValueError("Worktree changed during release review")
        if git(root, "rev-parse", "HEAD").decode().strip() != commit:
            raise ValueError("HEAD changed during release review")
        summary = json.loads((review / "plan-summary.json").read_text(encoding="utf-8"))
        report = {
            **verified,
            "reviewed_at": datetime.now(UTC).isoformat(),
            "plan_sha256": hashlib.sha256((review / "review.tfplan").read_bytes()).hexdigest(),
            "plan_summary": summary,
            "verified_blobs": blob_report["verified_blobs"],
            "apply_performed": False,
            "requires_changes": any(summary.values()),
        }
        # Success marker appears last. Failed attempts retain evidence without this marker.
        with (review / "review.json").open("x", encoding="utf-8") as output:
            output.write(json.dumps(report, indent=2) + "\n")
        return review / "review.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive", type=Path, required=True, help="Inner source ZIP, not artifact wrapper"
    )
    parser.add_argument("--commit", required=True, help="Full expected commit SHA")
    parser.add_argument(
        "--verify-only", action="store_true", help="No infrastructure access or backup"
    )
    args = parser.parse_args()
    if args.verify_only:
        print(json.dumps(verify_delivery(ROOT, args.archive.resolve(), args.commit), indent=2))
    else:
        print(prepare_review(ROOT, args.archive.resolve(), args.commit))


if __name__ == "__main__":
    main()
