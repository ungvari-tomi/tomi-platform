"""Offline DAG construction with the pinned Airflow SDK in a disposable container."""

import os
import secrets
import subprocess
import time
from uuid import uuid4

import pytest

ENABLED = os.environ.get("RETAILFLOW_TEST_AIRFLOW_IMAGE") == "1"


@pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_AIRFLOW_IMAGE") != "1",
    reason="Build orchestration validation images and opt in",
)
def test_airflow_sdk_constructs_both_dags() -> None:
    script = """
import runpy
import psycopg2
import airflow
from airflow.configuration import conf
from retailflow.extraction import extract_orders, extract_rates
assert airflow.__version__ == '3.3.2'
assert conf.get('core', 'auth_manager').endswith('SimpleAuthManager')
namespace = runpy.run_path('/opt/airflow/dags/retailflow_extraction.py')
from datetime import UTC, datetime
requested_date = namespace['requested_rate_date']
queued = datetime(2026, 10, 31, 12, tzinfo=UTC)
assert requested_date(None, queued) == '2026-10-31'
assert requested_date(datetime(2026,10,30,17,tzinfo=UTC), queued) == '2026-10-30'
assert requested_date(None, datetime.fromisoformat('2026-10-31T00:30:00+02:00')) == '2026-10-30'
from types import SimpleNamespace
import retailflow.extraction as extraction
calls = []
def capture_rates(run_id, day):
    calls.append((run_id,day))
    return ['test-manifest']
extraction.scheduled_all_rates = capture_rates
callback = namespace['exchange_rates']().get_task('extract').python_callable
for extra, expected in [({}, '2026-10-31'), ({'data_interval_end': None}, '2026-10-31'),
                        ({'data_interval_end': datetime(2026,10,30,17,tzinfo=UTC)}, '2026-10-30')]:
    context = {'run_id': 'manual-check', 'dag_run': SimpleNamespace(run_after=queued), **extra}
    callback.__globals__['get_current_context'] = lambda: context
    assert callback() == {'manifests': ['test-manifest']}
    assert calls[-1] == ('manual-check', expected)
workflows = [('orders', 'retailflow_orders'), ('exchange_rates', 'retailflow_exchange_rates')]
for name, expected_id in workflows:
    dag = namespace[name]()
    assert dag.dag_id == expected_id
    assert dag.max_active_runs == 1
    assert dag.catchup is False
    assert dag.is_paused_upon_creation is True
    assert len(dag.tasks) == 3
    assert dag.get_task('extract').downstream_task_ids == {'load'}
    assert dag.get_task('load').downstream_task_ids == {'dbt_build'}
    assert dag.get_task('dbt_build').upstream_task_ids == {'load'}
    assert dag.get_task('dbt_build').trigger_rule == 'all_success'
    assert dag.tasks[0].retries == 2
print('PASS: both DAGs constructed with the real Airflow SDK and PostgreSQL driver')
"""
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            "retailflow-airflow:validation",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not ENABLED, reason="Opt in to disposable orchestration image tests")
def test_orders_image_serves_http_as_non_root() -> None:
    script = """
import os, subprocess, sys, time, httpx
assert os.getuid() == 10001
process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'retailflow.api:app',
                            '--host', '127.0.0.1', '--port', '8000'])
try:
    with httpx.Client(base_url='http://127.0.0.1:8000', timeout=2, trust_env=False) as client:
        for attempt in range(50):
            try:
                response = client.get('/health')
                response.raise_for_status()
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise AssertionError('API did not become ready')
        page = client.get('/v1/orders?limit=3').json()
        assert len(page['orders']) == 3
        assert page['dataset_version'] == 'orders-v3'
        assert page['interval_seconds'] == 600
finally:
    process.terminate()
    process.wait(timeout=10)
"""
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--entrypoint",
            "python",
            "retailflow-orders-api:validation",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not ENABLED, reason="Opt in to disposable PostgreSQL/Airflow bootstrap test")
def test_airflow_initializes_real_postgres_without_managed_resources() -> None:
    name = f"retailflow-test-{uuid4().hex}"
    postgres_image = (
        "postgres:17-bookworm@sha256:"
        # Official image digest, not a credential.
        # pragma: allowlist nextline secret
        "839fbb5a4942a7758dd93e18b42ea5649807491a0545b5eeeb8f0b8bb11857a5"
    )
    env = dict(
        os.environ,
        POSTGRES_PASSWORD=secrets.token_hex(24),
        RETAILFLOW_AIRFLOW_PASSWORD=secrets.token_hex(24),
    )
    env["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] = (
        f"postgresql+psycopg2://airflow:{secrets.token_hex(24)}@{name}:5432/airflow"
    )
    env["RETAILFLOW_POSTGRES_ADMIN_PASSWORD"] = env["POSTGRES_PASSWORD"]

    def docker(*args: str, check: bool = True):
        return subprocess.run(
            ["docker", *args], env=env, capture_output=True, text=True, timeout=120, check=check
        )

    docker("network", "create", "--internal", name)
    try:
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            name,
            "-e",
            "POSTGRES_USER=postgres",
            "-e",
            "POSTGRES_DB=airflow",
            "-e",
            "POSTGRES_PASSWORD",
            postgres_image,
        )
        # The image's temporary initialization server accepts Unix-socket connections
        # before the final server listens on TCP. Airflow requires the latter.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if (
                docker(
                    "exec",
                    name,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-p",
                    "5432",
                    "-U",
                    "postgres",
                    "-d",
                    "airflow",
                    check=False,
                ).returncode
                == 0
            ):
                break
            time.sleep(0.25)
        else:
            raise AssertionError("Disposable PostgreSQL did not become ready on TCP port 5432")
        result = docker(
            "run",
            "--rm",
            "--network",
            name,
            "-e",
            "RETAILFLOW_AIRFLOW_PASSWORD",
            "-e",
            "RETAILFLOW_POSTGRES_ADMIN_PASSWORD",
            "-e",
            "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN",
            "--entrypoint",
            "python",
            "retailflow-airflow:validation",
            "/opt/airflow/start.py",
            "init",
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        version = docker(
            "exec",
            name,
            "psql",
            "-U",
            "airflow",
            "-d",
            "airflow",
            "-tAc",
            "SELECT count(*) FROM alembic_version",
        ).stdout.strip()
        assert version == "1"
        denied = docker(
            "exec",
            name,
            "psql",
            "-U",
            "airflow",
            "-d",
            "airflow",
            "-c",
            "CREATE DATABASE must_be_denied",
            check=False,
        )
        assert denied.returncode != 0
        assert "permission denied" in denied.stderr
    finally:
        # UUID-named disposable resources only; no Terraform-owned volume is touched.
        docker("rm", "--force", "--volumes", name, check=False)
        docker("network", "rm", name, check=False)
