"""Explicit local inbox diagnostics; automatic emails come only from DAG failures."""

import argparse
import json

# Fixed local Python command; no shell or credentials.
import subprocess  # nosec B404
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from retailflow.alerts import send_capture

ROOT = Path(__file__).resolve().parents[1]


def monitor_report() -> dict:
    result = subprocess.run(  # nosec B603
        [sys.executable, "-m", "scripts.monitor"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode not in {0, 1, 2}:
        raise RuntimeError("Monitoring process unavailable")
    report = json.loads(result.stdout)
    if report["exit_code"] != result.returncode:
        raise ValueError("Monitor exit code and report disagree")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--test-email", action="store_true", help="Explicit synthetic inbox test")
    mode.add_argument("--dry-run", action="store_true", help="Report service health without email")
    args = parser.parse_args()
    try:
        if args.test_email:
            send_capture(
                "RetailFlow local test",
                "Synthetic local inbox connectivity test. No real failure occurred.",
                uuid4().hex,
                host="127.0.0.1",
            )
            result, code = {"notification": "test_sent"}, 0
        else:
            report = monitor_report()
            result, code = (
                {"notification": "not_sent", "monitor_status": report["status"]},
                report["exit_code"],
            )
    except Exception as error:
        result, code = {"notification": "failed", "error_type": type(error).__name__}, 3
    result.update(checked_at=datetime.now(UTC).isoformat(), exit_code=code)
    directory = ROOT / "artifacts/monitoring"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ("mail-event-" + uuid4().hex + ".json")).open(
        "x", encoding="utf-8"
    ) as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
