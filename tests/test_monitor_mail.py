import json
import os
import smtplib
import subprocess
import time
from email.message import EmailMessage
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from retailflow import alerts
from scripts import monitor_mail


def test_failure_event_is_sent_once_and_never_reminded(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(alerts, "send_capture", lambda *a, **k: sent.append((a, k)))
    assert (
        alerts.notify_failed_run("retailflow_orders", "private-run-id", tmp_path)["status"]
        == "sent"
    )
    assert (
        alerts.notify_failed_run("retailflow_orders", "private-run-id", tmp_path)["status"]
        == "suppressed"
    )
    alerts.notify_failed_run("retailflow_orders", "another-run", tmp_path)
    assert len(sent) == 2
    assert "private-run-id" not in str(sent)
    assert all("recovery" not in str(item) for item in sent)


def test_smtp_failure_leaves_event_retryable(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise OSError("private SMTP response")

    monkeypatch.setattr(alerts, "send_capture", fail)
    with pytest.raises(OSError):
        alerts.notify_failed_run("retailflow_orders", "run", tmp_path)
    assert not list(tmp_path.glob("*.json"))


def test_dag_callback_ignores_running_success_and_retry_events(monkeypatch):
    monkeypatch.setattr(alerts, "notify_failed_run", lambda *a: pytest.fail("Must not send"))
    for state in ["running", "queued", "success", "up_for_retry"]:
        alerts.dag_failure({"dag_run": SimpleNamespace(state=state)})
    alerts.dag_failure({})


def test_dag_callback_filters_context_and_sanitizes_failures(monkeypatch):
    seen = []
    monkeypatch.setattr(
        alerts, "notify_failed_run", lambda *a: seen.append(a) or {"status": "sent"}
    )
    run = SimpleNamespace(state="failed", dag_id="retailflow_orders", run_id="failure")
    alerts.dag_failure({"dag_run": run, "exception": "must-not-be-emailed"})
    assert seen[0][:2] == ("retailflow_orders", "failure")
    assert "must-not-be-emailed" not in str(seen)

    def fail(*args):
        raise RuntimeError("private SMTP response")

    monkeypatch.setattr(alerts, "notify_failed_run", fail)
    with pytest.raises(RuntimeError) as error:
        alerts.dag_failure({"dag_run": run})
    assert "private SMTP response" not in str(error.value)


def test_local_capture_rejects_external_hosts():
    with pytest.raises(ValueError):
        alerts.send_capture("failure", "body", "event", host="smtp.gmail.com")


def test_cli_requires_explicit_diagnostic_mode(monkeypatch):
    monkeypatch.setattr("sys.argv", ["monitor_mail"])
    with pytest.raises(SystemExit) as result:
        monitor_mail.main()
    assert result.value.code == 2


def test_dry_run_never_sends_even_when_health_check_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(monitor_mail, "ROOT", tmp_path)
    monkeypatch.setattr(
        monitor_mail, "monitor_report", lambda: {"status": "critical", "exit_code": 2}
    )
    monkeypatch.setattr(monitor_mail, "send_capture", lambda *a, **k: pytest.fail("Must not send"))
    monkeypatch.setattr("sys.argv", ["monitor_mail", "--dry-run"])
    with pytest.raises(SystemExit) as result:
        monitor_mail.main()
    assert result.value.code == 2
    assert json.loads(capsys.readouterr().out)["notification"] == "not_sent"


@pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_MAILPIT_IMAGE") != "1",
    reason="Opt in to disposable Mailpit SMTP/UI/persistence test",
)
def test_local_inbox_captures_alerts_and_persists_without_relay(monkeypatch, tmp_path):
    name = "retailflow-test-mail-" + uuid4().hex

    def docker(*args):
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, check=True, timeout=60
        ).stdout.strip()

    docker("network", "create", name)
    try:
        docker(
            "create",
            "--name",
            name,
            "--network",
            name,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "10001:10001",
            "--publish",
            "127.0.0.1::1025",
            "--publish",
            "127.0.0.1::8025",
            "--mount",
            "type=volume,target=/data",
            "retailflow-mailpit:validation",
        )
        docker("start", name)
        ports = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", name))
        if not ports.get("1025/tcp"):
            logs = subprocess.run(
                ["docker", "logs", name], capture_output=True, text=True, timeout=10
            )
            raise AssertionError("Disposable Mailpit failed to start: " + logs.stdout + logs.stderr)
        smtp = int(ports["1025/tcp"][0]["HostPort"])
        ui = int(ports["8025/tcp"][0]["HostPort"])
        assert all(
            mapping["HostIp"] == "127.0.0.1"
            for bindings in ports.values()
            if bindings
            for mapping in bindings
        )
        assert docker("inspect", "--format", "{{.HostConfig.NetworkMode}}", name) == name
        assert docker("inspect", "--format", "{{.Config.User}}", name) == "10001:10001"

        with httpx.Client(
            base_url=f"http://127.0.0.1:{ui}",
            headers={"Host": "127.0.0.1"},
            trust_env=False,
            timeout=2,
        ) as client:

            def ready():
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        response = client.get("/api/v1/messages")
                        response.raise_for_status()
                        return response.json()
                    except httpx.HTTPError:
                        time.sleep(0.25)
                raise AssertionError("Disposable Mailpit did not become ready")

            ready()
            original_send = alerts.send_capture
            monkeypatch.setattr(
                alerts,
                "send_capture",
                lambda subject, body, identity, **kwargs: original_send(
                    subject, body, identity, host="127.0.0.1", port=smtp
                ),
            )
            state = tmp_path / "events"
            alerts.notify_failed_run("retailflow_orders", "failed-run-one", state)
            alerts.notify_failed_run("retailflow_orders", "failed-run-one", state)
            alerts.notify_failed_run("retailflow_exchange_rates", "failed-run-two", state)
            messages = ready()
            assert messages["total"] == 2
            assert {message["Subject"] for message in messages["messages"]} == {
                "RetailFlow DAG failed: retailflow_orders",
                "RetailFlow DAG failed: retailflow_exchange_rates",
            }
            rejected = EmailMessage()
            rejected["From"], rejected["To"] = "monitor@retailflow.test", "outside@example.com"
            rejected.set_content("Must be rejected locally")
            with smtplib.SMTP("127.0.0.1", smtp, timeout=5) as connection:
                with pytest.raises(smtplib.SMTPRecipientsRefused):
                    connection.send_message(rejected)
            assert client.get(
                "/api/v1/messages", headers={"Host": "untrusted.example"}
            ).status_code in {400, 403}
            docker("restart", name)
            ports = json.loads(
                docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", name)
            )
            client.base_url = f"http://127.0.0.1:{ports['8025/tcp'][0]['HostPort']}"
            assert ready()["total"] == 2
    finally:
        subprocess.run(
            ["docker", "rm", "--force", "--volumes", name], capture_output=True, timeout=60
        )
        subprocess.run(["docker", "network", "rm", name], capture_output=True, timeout=60)
