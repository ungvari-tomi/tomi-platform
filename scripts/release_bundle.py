"""Package a clean, validated commit as a small local-deployment source delivery."""

import argparse
import hashlib
import json
import shutil

# Read-only local git metadata; no shell or credentials.
import subprocess  # nosec B404
import zipfile
from pathlib import Path

from scripts.ci_safety import source_files


def git(root: Path, *args: str) -> str:
    executable = shutil.which("git")
    if not executable:
        raise RuntimeError("Git executable is missing")
    return subprocess.run(  # nosec B603
        [executable, *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def package(root: Path, destination: Path) -> Path:
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Delivery requires a clean committed worktree")
    commit = git(root, "rev-parse", "HEAD")
    names = source_files(root)
    required = {
        "pyproject.toml",
        "uv.lock",
        "infra/local/main.tf",
        "infra/dbt/uv.lock",
        "infra/orchestration/airflow.Dockerfile",
        "dags/retailflow_extraction.py",
    }
    if not required.issubset(names):
        raise ValueError("Required platform source files are not committed")
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"retailflow-{commit}.zip"
    manifest = {"format_version": 1, "git_commit": commit, "files": {}}
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as bundle:

        def write(name: str, content: bytes) -> None:
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            bundle.writestr(entry, content)

        for name in names:
            content = (root / name).read_bytes()
            manifest["files"][name] = hashlib.sha256(content).hexdigest()
            write("source/" + name, content)
        write("release.json", json.dumps(manifest, sort_keys=True, indent=2).encode())
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (destination / (archive.name + ".sha256")).write_text(
        f"{checksum}  {archive.name}\n", encoding="utf-8"
    )
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/release"))
    args = parser.parse_args()
    print(package(Path(__file__).resolve().parents[1], args.output))


if __name__ == "__main__":
    main()
