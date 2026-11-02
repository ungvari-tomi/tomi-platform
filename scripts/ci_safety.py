"""Reject tracked operational data and detect unreviewed source secrets."""

import argparse
import json
import shutil

# Local git inventory only; fixed arguments, no shell.
import subprocess  # nosec B404
from pathlib import Path


def source_files(root: Path, *, include_untracked: bool = False) -> list[str]:
    executable = shutil.which("git")
    if not executable:
        raise RuntimeError("Git executable is missing")
    command = [executable, "ls-files", "-z", "--cached"]
    if include_untracked:
        command += ["--others", "--exclude-standard"]
    result = subprocess.run(command, cwd=root, capture_output=True, check=True)  # nosec B603
    names = sorted(set(result.stdout.decode("utf-8").rstrip("\0").split("\0")) - {""})
    for name in names:
        path = Path(name)
        forbidden_parts = {
            ".git",
            ".terraform",
            ".venv",
            "__pycache__",
            ".bootstrap",
            ".python-runtime",
            ".uv-cache",
            "artifacts",
            "data",
            "dbt_packages",
            "logs",
            "target",
        }
        forbidden = (
            path.is_absolute()
            or ".." in path.parts
            or bool(forbidden_parts & set(path.parts))
            or (path.name.startswith(".env") and path.name != ".env.example")
            or ".tfstate" in path.name
            or ".duckdb" in path.name
            or path.name.endswith(
                (
                    ".tfplan",
                    ".tfvars",
                    ".tfvars.json",
                    ".local.tf",
                    ".duckdb",
                    ".duckdb.wal",
                    ".pem",
                    ".pfx",
                    ".key",
                )
            )
            or path.name == "migration-summary.json"
            or (root / path).is_symlink()
        )
        if forbidden:
            raise ValueError(f"Operational/private file cannot be committed or delivered: {name}")
    return names


def scan(root: Path, names: list[str]) -> list[dict]:
    from detect_secrets.core.scan import scan_file
    from detect_secrets.settings import default_settings

    findings = []
    with default_settings():
        for name in names:
            for secret in scan_file(str(root / name)):
                findings.append({"file": name, "line": secret.line_number, "type": secret.type})
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-untracked", action="store_true", help="Local preparation only")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    names = source_files(root, include_untracked=args.include_untracked)
    findings = scan(root, names)
    print(json.dumps({"files_scanned": len(names), "findings": findings}, indent=2))
    if findings:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
