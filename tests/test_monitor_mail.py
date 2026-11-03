import json
import os
import smtplib
import subprocess
import time
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from uuid import uuid4

import httpx
import pytest

from scripts import monitor_mail

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def outcome(*, healthy=False):
    return {
        "checked_at": NOW.isoformat(),
        "exit_code": 0 if healthy else 1,
        "alerts": []
        if healthy
        else [{"component": "warehouse.orders", "code": "STALE", "severity": "warning"}],
        "observations": {"private": "must-not-be-emailed"},
    }


def test_alert_dedup_reminder_change_and_recovery(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(monitor_mail, "send_message", lambda *args: sent.append(args))
    state = tmp_path / "state.json"
    assert monitor_mail.notify(outcome(healthy=True), state, now=NOW)["status"] == "no_alerts"
    assert not state.exists()
    assert monitor_mail.notify(outcome(), state, now=NOW)["kind"] == "alert"
    assert (
        monitor_mail.notify(outcome(), state, now=NOW + timedelta(minutes=10))["status"]
        == "suppressed"
    )
    assert monitor_mail.notify(outcome(), state, now=NOW + timedelta(hours=6))["status"] == "sent"
    changed = outcome()
    changed["alerts"][0]["severity"] = "critical"
    changed["exit_code"] = 2
    assert (
        monitor_mail.notify(changed, state, now=NOW + timedelta(hours=6, minutes=10))["status"]
        == "sent"
    )
    assert (
        monitor_mail.notify(outcome(healthy=True), state, now=NOW + timedelta(hours=7))["kind"]
        == "recovery"
    )
    assert (
        monitor_mail.notify(outcome(healthy=True), state, now=NOW + timedelta(hours=8))["status"]
        == "no_alerts"
    )
    assert [args[0] for args in sent] == ["alert", "alert", "alert", "recovery"]
    assert "must-not-be-emailed" not in state.read_text()


def test_smtp_failure_does_not_advance_delivery_state(monkeypatch, tmp_path):
    def fail(*args):
        raise OSError("private server response")

    monkeypatch.setattr(monitor_mail, "send_message", fail)
    path = tmp_path / "state.json"
    with pytest.raises(OSError):
        monitor_mail.notify(outcome(), path, now=NOW)
    assert not path.exists()


def test_smtp_is_loopback_only_and_message_excludes_observations(monkeypatch):
    messages = []

    class SMTP:
        def __init__(self, host, port, **kwargs):
            assert host == "127.0.0.1" and port == 1025 and kwargs["timeout"] == 10

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def send_message(self, message):
            messages.append(message)
            return {}

    monkeypatch.setattr(monitor_mail.smtplib, "SMTP", SMTP)
    report = outcome()
    monitor_mail.send_message("alert", report["alerts"], report["checked_at"])
    assert messages[0]["To"] == "operator@retailflow.test"
    assert messages[0]["From"] == "monitor@retailflow.test"
    assert "STALE" in messages[0].get_content()
    assert "must-not-be-emailed" not in str(messages[0])


def test_header_injection_is_rejected_before_smtp():
    report = outcome()
    report["alerts"][0]["code"] = "STALE\r\nBcc: outside@example.com"
    with pytest.raises(ValueError, match="alert fields"):
        monitor_mail.send_message("alert", report["alerts"], report["checked_at"])


def test_dry_run_does_not_send_or_advance_state(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(monitor_mail, "ROOT", tmp_path)
    monkeypatch.setattr(monitor_mail, "monitor_report", outcome)
    monkeypatch.setattr(monitor_mail, "notify", lambda *a: pytest.fail("Dry run must not notify"))
    monkeypatch.setattr("sys.argv", ["monitor_mail", "--dry-run"])
    with pytest.raises(SystemExit) as result:
        monitor_mail.main()
    assert result.value.code == 1
    assert json.loads(capsys.readouterr().out)["notification"]["status"] == "dry_run"
    assert not (tmp_path / "artifacts/monitoring/mail-state.json").exists()


def test_delivery_error_has_distinct_exit_code_and_no_raw_response(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(monitor_mail, "ROOT", tmp_path)
    monkeypatch.setattr(monitor_mail, "monitor_report", outcome)

    def fail(*args):
        raise RuntimeError("private server response")

    monkeypatch.setattr(monitor_mail, "notify", fail)
    monkeypatch.setattr("sys.argv", ["monitor_mail"])
    with pytest.raises(SystemExit) as result:
        monitor_mail.main()
    assert result.value.code == 3
    output = capsys.readouterr().out
    assert "private server response" not in output
    assert json.loads(output)["notification"] == {"status": "failed", "error_type": "RuntimeError"}


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
            original_send = monitor_mail.send_message
            monkeypatch.setattr(
                monitor_mail,
                "send_message",
                lambda kind, alerts, checked: original_send(kind, alerts, checked, port=smtp),
            )
            state = tmp_path / "state.json"
            monitor_mail.notify(outcome(), state, now=NOW)
            monitor_mail.notify(outcome(), state, now=NOW + timedelta(minutes=10))
            monitor_mail.notify(outcome(healthy=True), state, now=NOW + timedelta(minutes=20))
            messages = ready()
            assert messages["total"] == 2
            assert {message["Subject"] for message in messages["messages"]} == {
                "RetailFlow local alert",
                "RetailFlow local recovery",
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
