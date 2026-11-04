"""Capture one local email per failed DAG run; no polling, reminders or recovery."""

import hashlib
import json
import logging
import os
import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

ALLOWED_DAGS = {"retailflow_orders", "retailflow_exchange_rates"}


def send_capture(subject: str, body: str, identity: str, *, host: str, port: int = 1025) -> None:
    if host not in {"127.0.0.1", "retailflow-mailpit"} or not 1 <= port <= 65535:
        raise ValueError("Only the approved local capture server is supported")
    message = EmailMessage()
    message["From"] = "monitor@retailflow.test"
    message["To"] = "operator@retailflow.test"
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.now(UTC))
    message["Message-ID"] = f"<{identity}@retailflow.test>"
    message.set_content(body)
    with smtplib.SMTP(host, port, timeout=10) as client:
        if client.send_message(message):
            raise RuntimeError("Local inbox refused notification")


def notify_failed_run(dag_id: str, run_id: str, directory: Path) -> dict:
    if dag_id not in ALLOWED_DAGS or not isinstance(run_id, str) or not 1 <= len(run_id) <= 1024:
        raise ValueError("Unexpected DAG failure event")
    identity = hashlib.sha256(json.dumps([dag_id, run_id]).encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = directory / (identity + ".json")
    with FileLock(str(receipt) + ".lock", timeout=5):
        if receipt.exists():
            record = json.loads(receipt.read_text(encoding="utf-8"))
            if record.get("version") != 1 or record.get("event") != identity:
                raise ValueError("Invalid delivery receipt; preserve for investigation")
            return {"status": "suppressed", "event": identity}
        now = datetime.now(UTC).isoformat()
        # Run IDs and exceptions can contain arbitrary/sensitive text: email only a digest.
        send_capture(
            "RetailFlow DAG failed: " + dag_id,
            f"DAG: {dag_id}\nState: failed\nEvent: {identity}\nObserved: {now}\n"
            "Inspect this failed run in the local Airflow UI.\n",
            identity,
            host="retailflow-mailpit",
        )
        temporary = directory / (identity + "." + uuid4().hex + ".tmp")
        with temporary.open("x", encoding="utf-8") as output:
            output.write(json.dumps({"version": 1, "event": identity, "sent_at": now}) + "\n")
        os.replace(temporary, receipt)
    return {"status": "sent", "event": identity}


def dag_failure(context: dict) -> None:
    run = context.get("dag_run")
    if run is None or getattr(run.state, "value", run.state) != "failed":
        return
    try:
        result = notify_failed_run(run.dag_id, run.run_id, Path("/opt/airflow/logs/failure-email"))
    except Exception as error:
        logging.getLogger(__name__).error("DAG failure email unavailable: %s", type(error).__name__)
        raise RuntimeError("DAG failure email unavailable; inspect local callback logs") from None
    logging.getLogger(__name__).info("DAG failure email: %s", result["status"])
