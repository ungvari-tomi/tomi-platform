"""Choose conservative CI coverage from Git paths; deliveries always validate fully."""

import argparse
import json
import os
import re
import shutil
import subprocess  # nosec B404
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def select(paths: list[str], *, full: bool = False) -> dict:
    flags = dict.fromkeys(
        ("checks", "terraform", "dependencies", "workflows", "integration"), False
    )
    targets = set()

    def everything() -> None:
        flags.update(dict.fromkeys(flags, True))
        targets.clear()
        targets.add("tests")

    if full or not paths:
        everything()
    for path in paths:
        if path == "AGENTS.md" or path == "README.md" or path.startswith("docs/"):
            continue
        if path == "retailflow/infrastructure.py":
            flags["checks"] = flags["terraform"] = True
            targets.update({"tests/test_infrastructure.py", "tests/test_release_review.py"})
        elif path in {".sqlfluff", ".sqlfluffignore"}:
            flags["checks"] = True
            targets.add("tests/test_sql_style.py")
        elif path == "scripts/release_review.py":
            flags["checks"] = True
            targets.update({"tests/test_release_review.py", "tests/test_infrastructure.py"})
        elif path in {"scripts/release_bundle.py", "scripts/ci_safety.py"}:
            flags["checks"] = True
            targets.update({"tests/test_ci_delivery.py", "tests/test_release_review.py"})
        elif path == "scripts/ci_select.py":
            flags["checks"] = flags["workflows"] = True
            targets.add("tests/test_ci_select.py")
        elif path in {"scripts/monitor.py", "scripts/monitor_warehouse.py"}:
            flags["checks"] = True
            targets.add("tests/test_monitor.py")
        elif path == "scripts/monitor_mail.py":
            flags["checks"] = True
            targets.add("tests/test_monitor_mail.py")
        elif path.startswith("infra/local/") and path.endswith((".tf", ".hcl")):
            flags["checks"] = flags["terraform"] = True
            targets.add("tests/test_infrastructure.py")
        elif path == "tests/test_image_inputs.py":
            flags["checks"] = flags["terraform"] = True
            targets.add(path)
        elif path.startswith("tests/test_") and path.endswith(".py"):
            flags["checks"] = True
            if (ROOT / path).is_file():
                targets.add(path)
            else:
                everything()  # Deleted/renamed test: include remaining contracts.
            # Docker tests opt in at runtime; do not silently skip their changes.
            text = (ROOT / path).read_text(encoding="utf-8") if (ROOT / path).is_file() else ""
            if any(
                marker in text
                for marker in (
                    "RETAILFLOW_TEST_AIRFLOW_IMAGE",
                    "RETAILFLOW_TEST_DBT_IMAGE",
                    "RETAILFLOW_TEST_DUCKDB_IMAGE",
                    "RETAILFLOW_TEST_AZURITE",
                    "RETAILFLOW_TEST_MAILPIT_IMAGE",
                )
            ):
                flags["integration"] = True
        else:
            # Runtime, build inputs, locks, workflows, fixtures and unknown files
            # have broad dependencies. Prefer extra coverage over an unsafe skip.
            everything()
    if "tests" in targets:
        targets = {"tests"}
    return {**flags, "tests": sorted(targets)}


def changed_paths(event: dict, head: str) -> list[str] | None:
    before = event.get("pull_request", {}).get("base", {}).get("sha") or event.get("before")
    if not before or not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (before, head)):
        return None
    if before == "0" * 40:
        return None
    executable = shutil.which("git")
    if not executable:
        return None
    result = subprocess.run(  # nosec B603
        [executable, "diff", "--no-renames", "--name-only", "-z", before, head],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode:
        return None
    return result.stdout.decode("utf-8").rstrip("\0").split("\0") if result.stdout else []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-tests", action="store_true")
    parser.add_argument("--junitxml")
    args = parser.parse_args()
    if args.run_tests:
        import pytest

        targets = json.loads(os.environ["RETAILFLOW_SELECTED_TESTS"])
        if not targets or not all(
            isinstance(path, str)
            and (path == "tests" or re.fullmatch(r"tests/test_[a-z0-9_]+\.py", path))
            for path in targets
        ):
            raise ValueError("Invalid selected test paths")
        arguments = ["-q", "-p", "no:cacheprovider", *targets]
        if args.junitxml:
            arguments.append("--junitxml=" + args.junitxml)
        raise SystemExit(pytest.main(arguments))
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    full = os.environ.get("RETAILFLOW_FULL_VALIDATION") == "true" or os.environ.get(
        "GITHUB_EVENT_NAME"
    ) not in {"push", "pull_request"}
    paths = None if full else changed_paths(event, os.environ.get("GITHUB_SHA", ""))
    selection = select(paths or [], full=full or paths is None)
    print(json.dumps(selection, indent=2))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for key, value in selection.items():
            output.write(f"{key}={json.dumps(value, separators=(',', ':'))}\n")


if __name__ == "__main__":
    main()
