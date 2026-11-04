import json
from datetime import UTC, datetime, timedelta

import duckdb
import httpx
import pytest
from filelock import FileLock, Timeout

from scripts import monitor, monitor_warehouse

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


@pytest.fixture
def observations():
    return {
        "source": {"latest_available_at": NOW.isoformat(), **monitor.Config().identity()},
        "warehouse": {"status": "ok"},
        "airflow": {
            "health": {key: "healthy" for key in ["metadatabase", "scheduler", "dag_processor"]},
            "dags": {
                dag: {
                    "paused": False,
                    "last_success_at": (NOW - timedelta(minutes=10)).isoformat(),
                    "latest_completed_state": "success",
                    "oldest_active_at": None,
                }
                for dag in monitor.DAGS
            },
        },
    }


def test_paused_failed_stuck_and_missing_success_are_visible(observations):
    dag = observations["airflow"]["dags"]["retailflow_orders"]
    dag.update(
        paused=True,
        latest_completed_state="failed",
        last_success_at=None,
        oldest_active_at=(NOW - timedelta(minutes=31)).isoformat(),
    )
    report = monitor.evaluate(observations, monitor.Config(), NOW)
    assert report["exit_code"] == 2
    assert {item["code"] for item in report["alerts"]} == {
        "DAG_PAUSED",
        "LATEST_RUN_FAILED",
        "RUN_STUCK",
    }


def test_source_identity_mismatch_and_unhealthy_scheduler_are_critical(observations):
    observations["source"]["seed"] = 99
    observations["airflow"]["health"]["scheduler"] = "unhealthy"
    report = monitor.evaluate(observations, monitor.Config(), NOW)
    assert report["exit_code"] == 2
    assert {item["code"] for item in report["alerts"]} == {
        "SOURCE_CONFIGURATION_MISMATCH",
        "UNHEALTHY",
    }


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com",
        "http://192.168.1.1",
        "http://127.0.0.1@evil.example",
        "http://localhost/path",
        "http://localhost/?key=value",
    ],
)
def test_monitor_rejects_non_loopback_origins_and_credential_urls(value):
    with pytest.raises(ValueError):
        monitor.loopback_url(value)


def test_configuration_environment_overrides_working_directory_dotenv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("RETAILFLOW_MONITOR_RUN_MAX_AGE_SECONDS=2400\n")
    monkeypatch.setenv("RETAILFLOW_MONITOR_RUN_MAX_AGE_SECONDS", "3600")
    assert monitor.Config.load().run_max_age == 3600
    monkeypatch.delenv("RETAILFLOW_MONITOR_RUN_MAX_AGE_SECONDS")
    assert monitor.Config.load().run_max_age == 2400
    # Synthetic test value used only to verify credential redaction.
    # pragma: allowlist nextline secret
    assert "private-value" not in repr(monitor.Config(password="private-value"))


def test_probe_failure_never_outputs_exception_secrets(monkeypatch, observations):
    def fail(config):
        raise RuntimeError("private-value")

    monkeypatch.setattr(monitor, "source_probe", fail)
    monkeypatch.setattr(monitor, "airflow_probe", lambda config: observations["airflow"])
    monkeypatch.setattr(monitor, "warehouse_probe", lambda config: observations["warehouse"])
    report = monitor.check(monitor.Config(password="private-value"))
    assert any(item["code"] == "PROBE_UNAVAILABLE" for item in report["alerts"])
    assert "private-value" not in json.dumps(report)


def test_airflow_filters_success_failure_and_active_runs_without_mutating_dags(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "synthetic-token"})
        assert request.method == "GET"
        if request.url.path.endswith("/health"):
            return httpx.Response(
                200,
                json={
                    key: {"status": "healthy"}
                    for key in ["metadatabase", "scheduler", "dag_processor"]
                },
            )
        if request.url.path.endswith("/dagRuns"):
            states = request.url.params.get_list("state")
            if states == ["success"]:
                return httpx.Response(200, json={"dag_runs": [{"end_date": NOW.isoformat()}]})
            if states == ["success", "failed"]:
                return httpx.Response(200, json={"dag_runs": [{"state": "success"}]})
            assert states == ["queued", "running"]
            return httpx.Response(200, json={"dag_runs": []})
        return httpx.Response(200, json={"is_paused": False})

    client = httpx.Client(transport=httpx.MockTransport(handle), base_url="http://127.0.0.1:8080")
    monkeypatch.setattr(monitor.httpx, "Client", lambda **kwargs: client)
    data = monitor.airflow_probe(monitor.Config(password="private-value"))
    assert data["dags"]["retailflow_orders"]["latest_completed_state"] == "success"
    assert "synthetic-token" not in json.dumps(data) and "private-value" not in json.dumps(data)
    assert len([request for request in requests if request.method == "POST"]) == 1


@pytest.fixture
def warehouse(tmp_path):
    path = tmp_path / "retailflow.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE SCHEMA raw; CREATE SCHEMA ops")
        connection.execute(
            """CREATE TABLE ops.datasets(dataset_id VARCHAR, dataset_version VARCHAR,
               seed BIGINT, interval_seconds BIGINT)"""
        )
        connection.execute(
            """INSERT INTO ops.datasets VALUES
               ('current','orders-v3',42,600),('old','orders-v2',42,600)"""
        )
        connection.execute("CREATE TABLE raw.orders(dataset_id VARCHAR, created_at TIMESTAMPTZ)")
        connection.execute(
            """INSERT INTO raw.orders VALUES
               ('current','2026-11-02T12:00:00Z'),('old','2026-11-03T00:00:00Z')"""
        )
        connection.execute(
            """CREATE TABLE raw.exchange_rates(provider VARCHAR, base VARCHAR,
               quote VARCHAR, rate_date DATE)"""
        )
        connection.execute("INSERT INTO raw.exchange_rates VALUES ('ECB','EUR','HUF','2026-10-30')")
    return path


def test_locked_warehouse_probe_reads_only_current_identity_and_preserves_records(warehouse):
    original = warehouse.read_bytes()
    data = monitor_warehouse.collect(warehouse)
    assert data == {"status": "ok"}
    assert warehouse.read_bytes() == original


def test_warehouse_probe_respects_existing_writer_lock(monkeypatch, warehouse):
    monkeypatch.setattr(
        monitor_warehouse,
        "FileLock",
        lambda path, **kwargs: FileLock(path, mode=kwargs["mode"], timeout=0),
    )
    with FileLock(str(warehouse) + ".lock", mode=0o660):
        with pytest.raises(Timeout):
            monitor_warehouse.collect(warehouse)


def test_probe_never_initializes_an_absent_warehouse(tmp_path):
    path = tmp_path / "absent.duckdb"
    with pytest.raises(FileNotFoundError):
        monitor_warehouse.collect(path)
    assert not path.exists()


def test_busy_warehouse_is_not_reported_as_healthy_or_unavailable(observations):
    observations["warehouse"] = {"status": "busy"}
    report = monitor.evaluate(observations, monitor.Config(), NOW)
    assert report["exit_code"] == 1
    assert report["alerts"] == [
        {"component": "warehouse", "code": "WAREHOUSE_BUSY", "severity": "warning"}
    ]


def test_authentication_redirect_is_not_followed(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://external.example/"})

    client = httpx.Client(transport=httpx.MockTransport(handle), base_url="http://127.0.0.1:8080")

    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        return client

    monkeypatch.setattr(monitor.httpx, "Client", factory)
    with pytest.raises(httpx.HTTPStatusError):
        # Synthetic test value, never an actual service credential.
        # pragma: allowlist nextline secret
        monitor.airflow_probe(monitor.Config(password="private-value"))
    assert len(requests) == 1 and requests[0].url.host == "127.0.0.1"


@pytest.mark.parametrize("code", [0, 1, 2])
def test_cli_prints_json_and_returns_monitor_exit_code(monkeypatch, capsys, code):
    monkeypatch.setattr(monitor.Config, "load", classmethod(lambda cls: monitor.Config()))
    monkeypatch.setattr(monitor, "check", lambda config: {"exit_code": code, "alerts": []})
    monkeypatch.setattr("sys.argv", ["monitor", "--no-save"])
    with pytest.raises(SystemExit) as result:
        monitor.main()
    assert result.value.code == code
    report = json.loads(capsys.readouterr().out)
    assert report["exit_code"] == code and "report_path" not in report


def test_report_files_are_unique_and_exclude_credentials(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(monitor, "ROOT", tmp_path)
    monkeypatch.setattr(
        monitor.Config, "load", classmethod(lambda cls: monitor.Config(password="private-value"))
    )
    monkeypatch.setattr(monitor, "check", lambda config: {"exit_code": 0, "alerts": []})
    monkeypatch.setattr("sys.argv", ["monitor"])
    for _ in range(2):
        with pytest.raises(SystemExit):
            monitor.main()
    files = list((tmp_path / "artifacts/monitoring").glob("*.json"))
    assert len(files) == 2
    assert all("private-value" not in path.read_text() for path in files)
    assert "private-value" not in capsys.readouterr().out


def test_data_freshness_is_not_reimplemented_in_health_monitor(observations):
    observations["source"]["latest_available_at"] = "2000-01-01T00:00:00Z"
    observations["warehouse"]["latest_order_at"] = "2000-01-01T00:00:00Z"
    for dag in observations["airflow"]["dags"].values():
        dag["last_success_at"] = "2000-01-01T00:00:00Z"
    assert monitor.evaluate(observations, monitor.Config(), NOW)["exit_code"] == 0
