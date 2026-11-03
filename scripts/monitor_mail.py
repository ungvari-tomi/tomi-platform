"""Deliver monitor alerts to a loopback-only local inbox, never an email provider."""

import argparse
import hashlib
import json
import os
import re
import smtplib

# Fixed local Python command, no shell or credential arguments.
import subprocess  # nosec B404
import sys
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

ROOT = Path(__file__).resolve().parents[1]
REMINDER_SECONDS = 21600


def send_message(kind: str, alerts: list[dict], checked_at: str, *, port: int = 1025) -> None:
    if kind not in {"alert", "recovery", "test"} or not 1 <= port <= 65535:
        raise ValueError("Invalid local notification")
    timestamp = datetime.fromisoformat(checked_at)
    if timestamp.utcoffset() is None:
        raise ValueError("Notification timestamps must have a timezone")
    lines = ["RetailFlow local monitoring", f"Checked: {timestamp.astimezone(UTC).isoformat()}"]
    for alert in alerts:
        fields = [alert[key] for key in ("severity", "component", "code")]
        if not all(
            isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value)
            for value in fields
        ):
            raise ValueError("Invalid alert fields")
        lines.append(" | ".join(fields))
    if kind == "recovery":
        lines.append("All monitored checks passed after a previously emailed alert.")
    elif kind == "test":
        lines.append("Synthetic local inbox connectivity test. No real pipeline failure occurred.")
    message = EmailMessage()
    message["From"] = "monitor@retailflow.test"
    message["To"] = "operator@retailflow.test"
    message["Subject"] = "RetailFlow local " + kind
    message["Date"] = format_datetime(datetime.now(UTC))
    message["Message-ID"] = make_msgid(domain="retailflow.test")
    message.set_content("\n".join(lines))
    # No host/recipient/authentication configuration: this sends only to the local catcher.
    with smtplib.SMTP("127.0.0.1", port, timeout=10) as client:
        if client.send_message(message):
            raise RuntimeError("Local inbox refused the recipient")


def notify(report: dict, state_path: Path, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    if report["exit_code"] not in {0, 1, 2} or (report["exit_code"] == 0) != (not report["alerts"]):
        raise ValueError("Invalid monitor outcome")
    alerts = report["alerts"]
    fingerprint = hashlib.sha256(
        json.dumps(
            sorted((item["component"], item["code"], item["severity"]) for item in alerts)
        ).encode()
    ).hexdigest()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(state_path) + ".lock", timeout=0):
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if state and state.get("version") != 1:
            raise ValueError("Unrecognized notification state; preserve it for inspection")
        if not alerts and not state.get("active"):
            return {"status": "no_alerts"}
        if alerts and fingerprint == state.get("fingerprint"):
            last_sent = datetime.fromisoformat(state["sent_at"])
            if last_sent.utcoffset() is None or now < last_sent:
                raise ValueError("Invalid notification state timestamp")
            if (now - last_sent).total_seconds() < REMINDER_SECONDS:
                return {"status": "suppressed"}
        kind = "alert" if alerts else "recovery"
        send_message(kind, alerts, report.get("checked_at", now.isoformat()))
        # Advance deduplication state only after SMTP accepted the message.
        state = {
            "version": 1,
            "active": bool(alerts),
            "fingerprint": fingerprint,
            "sent_at": now.isoformat(),
        }
        temporary = state_path.with_name(state_path.name + "." + uuid4().hex + ".tmp")
        with temporary.open("x", encoding="utf-8") as output:
            output.write(json.dumps(state, indent=2) + "\n")
        os.replace(temporary, state_path)
        return {"status": "sent", "kind": kind}


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
    parser.add_argument(
        "--test-email", action="store_true", help="Send a synthetic local inbox test"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Do not send or advance delivery state"
    )
    args = parser.parse_args()
    if args.test_email and args.dry_run:
        parser.error("Choose test-email or dry-run")
    directory = ROOT / "artifacts/monitoring"
    directory.mkdir(parents=True, exist_ok=True)
    report = {}
    try:
        if args.test_email:
            send_message("test", [], datetime.now(UTC).isoformat())
            event, exit_code = {"status": "sent", "kind": "test"}, 0
        else:
            report = monitor_report()
            event = (
                {"status": "dry_run"}
                if args.dry_run
                else notify(report, directory / "mail-state.json")
            )
            exit_code = report["exit_code"]
    except Exception as error:  # Never print SMTP responses, report payloads or raw exceptions.
        event, exit_code = {"status": "failed", "error_type": type(error).__name__}, 3
    event = {
        "checked_at": datetime.now(UTC).isoformat(),
        "notification": event,
        "monitor_status": report.get("status"),
        "exit_code": exit_code,
    }
    path = directory / ("mail-event-" + uuid4().hex + ".json")
    with path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(event, indent=2) + "\n")
    print(json.dumps(event, indent=2))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
