"""Evaluate the real Terraform hash expressions in a provider-free disposable tree."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TERRAFORM = ROOT / ".bootstrap/terraform/terraform.exe"
EXECUTABLE = str(TERRAFORM) if TERRAFORM.is_file() else shutil.which("terraform")


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
