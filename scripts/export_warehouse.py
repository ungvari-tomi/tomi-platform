"""Export a verified, independent warehouse snapshot for desktop SQL tools."""

import argparse
import json
import shutil

# Fixed local Docker commands use argument lists and never invoke a shell.
import subprocess  # nosec B404
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from infra.duckdb.recovery import restore

ROOT = Path(__file__).resolve().parents[1]


def docker(*arguments: str) -> str:
    executable = shutil.which("docker")
    if not executable:
        raise RuntimeError("Docker CLI is missing")
    result = subprocess.run(  # nosec B603
        [executable, *arguments], capture_output=True, text=True, check=True, timeout=180
    )
    return result.stdout


def export_snapshot(directory: Path, *, container: str = "retailflow-duckdb") -> dict:
    # The deployed backup command checkpoints/closes under the common writer lock.
    backup = json.loads(docker("exec", container, "python", "/app/runner.py", "backup"))
    remote = PurePosixPath(backup["backup"])
    if remote.parent != PurePosixPath("/backups") or remote.name.startswith("."):
        raise ValueError("Unexpected completed backup path")
    identity = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex
    destination = directory.resolve() / identity
    destination.mkdir(parents=True, mode=0o700)
    bundle = destination / "bundle"
    docker("cp", f"{container}:{remote}", str(bundle))
    # Restore validates transferred bytes and SQL inventory, never overwrites a target.
    # Preserve the managed catalog name for fully qualified dbt view references.
    verified = restore(bundle, destination / "retailflow.duckdb")
    report = {
        "snapshot": verified["target"],
        "captured_at": backup["created_at"],
        "exported_at": datetime.now(UTC).isoformat(),
        "status": verified["status"],
        "duckdb_version": verified["duckdb_version"],
        "tables": verified["tables"],
        "source_backup": str(remote),
    }
    # Success marker is written only after checksum and SQL verification.
    (destination / "snapshot.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-directory", type=Path, default=ROOT / "artifacts/warehouse-snapshots"
    )
    parser.add_argument("--container", default="retailflow-duckdb")
    args = parser.parse_args()
    report = export_snapshot(args.output_directory, container=args.container)
    print(json.dumps(report, indent=2))
    print("\nOpen this database file in DBeaver:\n" + report["snapshot"])


if __name__ == "__main__":
    main()
