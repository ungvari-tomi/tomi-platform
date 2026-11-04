"""Invoke the registered DAG failure callback over Docker DNS into disposable Mailpit."""

import json
import os
import subprocess
import time
from uuid import uuid4

import httpx
import pytest


@pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_AIRFLOW_IMAGE") != "1"
    or os.environ.get("RETAILFLOW_TEST_MAILPIT_IMAGE") != "1",
    reason="Opt in to disposable Airflow/Mailpit image integration",
)
def test_dag_failure_callback_captures_one_email_per_run_over_docker_dns():
    name = "failure-mail-" + uuid4().hex
    worker = name + "-worker"

    def docker(*args):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    docker("network", "create", name)
    try:
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            name,
            "--network-alias",
            "retailflow-mailpit",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--publish",
            "127.0.0.1::8025",
            "--mount",
            "type=volume,target=/data",
            "retailflow-mailpit:validation",
        )
        ports = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", name))
        port = ports["8025/tcp"][0]["HostPort"]
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Host": "127.0.0.1"},
            trust_env=False,
            timeout=2,
        ) as client:
            for _ in range(40):
                try:
                    client.get("/api/v1/messages").raise_for_status()
                    break
                except httpx.HTTPError:
                    time.sleep(0.25)
            script = """
import runpy
from pathlib import Path
from types import SimpleNamespace
dag=runpy.run_path('/opt/airflow/dags/retailflow_extraction.py')['orders']()
run=SimpleNamespace(state='running',dag_id=dag.dag_id,run_id='disposable-failure-one')
context={'dag_run':run,'exception':'must-not-be-emailed'}
dag.on_failure_callback(context)
run.state='failed'
dag.on_failure_callback(context)
dag.on_failure_callback(context)
run.state='success'
dag.on_failure_callback(context)
run.state='failed'; run.run_id='disposable-failure-two'
dag.on_failure_callback(context)
assert len(list(Path('/opt/airflow/logs/failure-email').glob('*.json')))==2
"""
            docker(
                "run",
                "--name",
                worker,
                "--network",
                name,
                "--read-only",
                "--mount",
                "type=volume,target=/opt/airflow/logs",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--entrypoint",
                "python",
                "retailflow-airflow:validation",
                "-c",
                script,
            )
            response = client.get("/api/v1/messages")
            response.raise_for_status()
            messages = response.json()
            assert messages["total"] == 2
            assert all(
                message["Subject"] == "RetailFlow DAG failed: retailflow_orders"
                for message in messages["messages"]
            )
            for message in messages["messages"]:
                detail = client.get("/api/v1/message/" + message["ID"]).json()
                assert "must-not-be-emailed" not in detail["Text"]
                assert "disposable-failure" not in detail["Text"]
    finally:
        for container in [worker, name]:
            subprocess.run(
                ["docker", "rm", "--force", "--volumes", container], capture_output=True, timeout=60
            )
        subprocess.run(["docker", "network", "rm", name], capture_output=True, timeout=60)
