"""Evaluate the real Terraform hash expressions in a provider-free disposable tree."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TERRAFORM = ROOT / ".bootstrap/terraform/terraform.exe"
EXECUTABLE = str(TERRAFORM) if TERRAFORM.is_file() else shutil.which("terraform")


@pytest.mark.skipif(not EXECUTABLE, reason="Terraform CLI is required")
@pytest.mark.parametrize("provider,allowed", [("mock", True), ("ollama", True), ("openai", False)])
def test_analyst_terraform_rejects_paid_providers(tmp_path, provider, allowed):
    source = (ROOT / "infra/local/analyst.tf").read_text(encoding="utf-8")
    variable = (
        'variable "analyst_provider" {'
        + source.split('variable "analyst_provider" {', 1)[1].split('variable "analyst_model"', 1)[
            0
        ]
    )
    (tmp_path / "main.tf").write_text(variable, encoding="utf-8")
    result = subprocess.run(
        [
            EXECUTABLE,
            f"-chdir={tmp_path}",
            "plan",
            "-input=false",
            "-no-color",
            f"-var=analyst_provider={provider}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) == allowed, result.stdout + result.stderr
    if not allowed:
        assert "Free-only platform" in result.stderr


@pytest.mark.skipif(not EXECUTABLE, reason="Terraform CLI is required for real hash evaluation")
def test_reporting_module_rebuilds_both_publisher_and_dashboard(tmp_path):
    module = tmp_path / "infra/local"
    module.mkdir(parents=True)
    source = tmp_path / "infra/dbt/reporting.py"
    source.parent.mkdir()
    source.write_bytes((ROOT / "infra/dbt/reporting.py").read_bytes())
    expressions = {}
    for key, filename, field in [
        ("publisher", "orchestration.tf", "reporting"),
        ("reader", "dashboard.tf", "reader"),
    ]:
        lines = (ROOT / "infra/local" / filename).read_text(encoding="utf-8").splitlines()
        expressions[key] = next(
            line.split("=", 1)[1].strip()
            for line in lines
            if line.strip().split("=", 1)[0].strip() == field
        )
    (module / "main.tf").write_text(
        "locals {\n" + "\n".join(f"{k} = {v}" for k, v in expressions.items()) + "\n}\n"
    )

    def hashes():
        result = subprocess.run(
            [EXECUTABLE, f"-chdir={module}", "console", "-no-color"],
            input="jsonencode({publisher=local.publisher,reader=local.reader})\n",
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return json.loads(json.loads(result.stdout))

    before = hashes()
    with source.open("a") as output:
        output.write("\n# isolated source edit\n")
    after = hashes()
    assert before["publisher"] == before["reader"]
    assert all(before[key] != after[key] for key in before)


@pytest.mark.skipif(not EXECUTABLE, reason="Terraform CLI is required for real path evaluation")
@pytest.mark.parametrize(
    ("host_path", "expected"),
    [
        (
            "C:/Users/Example User/.local-secrets/key",
            "/run/desktop/mnt/host/c/Users/Example User/.local-secrets/key",
        ),
        ("D:/Users/Ungvári Tamás/key", "/run/desktop/mnt/host/d/Users/Ungvári Tamás/key"),
        ("/home/Example User/key", "/home/Example User/key"),
    ],
)
def test_shipment_bind_path_matches_docker_desktop_without_changing_linux_paths(
    tmp_path, host_path, expected
):
    source = (ROOT / "infra/local/parallel.tf").read_text(encoding="utf-8")
    expression = next(
        line
        for line in source.splitlines()
        if line.strip().startswith("shipment_key_mount_source =")
    )
    (tmp_path / "main.tf").write_text(
        "locals {\n shipment_key_path = " + json.dumps(host_path) + "\n" + expression + "\n}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [EXECUTABLE, f"-chdir={tmp_path}", "console", "-no-color"],
        input="jsonencode(local.shipment_key_mount_source)\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    assert json.loads(json.loads(result.stdout)) == expected


@pytest.mark.skipif(not EXECUTABLE, reason="Terraform CLI is required for real hash evaluation")
def test_runtime_hashes_ignore_local_helpers_but_track_runtime_changes(tmp_path):
    module = tmp_path / "infra/local"
    module.mkdir(parents=True)
    # Reuse the actual locals rather than reimplementing Terraform hashing in Python.
    source = (ROOT / "infra/local/orchestration.tf").read_text(encoding="utf-8")
    locals_source = source.split("locals {", 1)[1].split("  airflow_env =", 1)[0]
    (module / "main.tf").write_text("locals {" + locals_source + "}\n", encoding="utf-8")
    shutil.copytree(
        ROOT / "retailflow", tmp_path / "retailflow", ignore=shutil.ignore_patterns("__pycache__")
    )

    def hashes():
        result = subprocess.run(
            [EXECUTABLE, f"-chdir={module}", "console", "-no-color"],
            input="jsonencode({api=local.api_application_hash,airflow=local.application_hash})\n",
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return json.loads(json.loads(result.stdout))

    def edit(name):
        with (tmp_path / "retailflow" / name).open("a", encoding="utf-8") as output:
            output.write("\n# simulated isolated source change\n")

    original = hashes()
    edit("infrastructure.py")
    assert hashes() == original
    edit("warehouse.py")
    warehouse = hashes()
    assert warehouse["airflow"] != original["airflow"]
    assert warehouse["api"] == original["api"]
    edit("currencies.py")
    currencies = hashes()
    assert currencies["airflow"] != warehouse["airflow"]
    assert currencies["api"] != warehouse["api"]
    edit("customers.py")
    customers = hashes()
    assert customers["airflow"] != currencies["airflow"]
    assert customers["api"] != currencies["api"]
