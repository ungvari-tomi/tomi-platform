"""Operate local Terraform with secret injection and a blob-preserving migration plan."""

import argparse
import base64
import hashlib
import json
import os
import secrets
import shutil

# Required for fixed Docker/Terraform CLI operations; calls use argument lists, never a shell.
import subprocess  # nosec B404
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

from retailflow.blob_storage import BlobStore

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "infra/local"
BACKUP = ROOT / "artifacts/terraform-migration"


def initialize_orchestration() -> None:
    local = dotenv_values(ROOT / ".env", interpolate=False)
    values = {
        "AIRFLOW_DB_PASSWORD": secrets.token_urlsafe(32),
        "AIRFLOW_POSTGRES_ADMIN_PASSWORD": secrets.token_urlsafe(32),
        "RETAILFLOW_AIRFLOW_PASSWORD": secrets.token_urlsafe(24),
        "AIRFLOW_FERNET_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "AIRFLOW_JWT_SECRET": secrets.token_urlsafe(32),
    }
    with (ROOT / ".env").open("a", encoding="utf-8") as handle:
        for name, value in values.items():
            if not os.environ.get(name) and not local.get(name):
                handle.write(f"\n{name}={value}\n")


def run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    # Commands/arguments are constructed here for fixed local Docker/Terraform operations.
    return subprocess.run(  # nosec B603
        command, cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )


def terraform(env: dict[str, str], *args: str) -> str:
    binary = ROOT / ".bootstrap/terraform/terraform.exe"
    executable = str(binary) if binary.is_file() else shutil.which("terraform")
    if not executable:
        raise ValueError("Install Terraform 1.17.1 or use the project-local bootstrap binary")
    result = run([executable, f"-chdir={DIRECTORY}", *args], env)
    if result.returncode:
        message = result.stdout + result.stderr
        for name, value in env.items():
            if (
                name.startswith("TF_VAR_")
                and any(word in name for word in ("key", "password", "secret"))
                and value
            ):
                message = message.replace(value, "<REDACTED>")
        raise ValueError(message)
    return result.stdout


def prepare_imports(env: dict[str, str]) -> None:
    """Discover local IDs; never put another developer's container ID in source."""
    resources = [
        ("volume", "tomi-platform_azurite-data", "docker_volume.azurite"),
        ("network", "tomi-platform_default", "docker_network.azurite"),
        ("container", "tomi-platform-azurite-1", "docker_container.azurite"),
    ]
    blocks = []
    for kind, name, address in resources:
        attribute = "Name" if kind == "volume" else "Id"
        result = run(["docker", kind, "inspect", "--format", f"{{{{.{attribute}}}}}", name], env)
        if result.returncode:
            if "no such" in result.stderr.lower():
                continue  # Fresh installation: Terraform will create this resource.
            raise ValueError(f"Cannot inspect local Docker {kind}; check Docker Desktop access")
        blocks.append(
            f"import {{\n  to = {address}\n  id = {json.dumps(result.stdout.strip())}\n}}\n"
        )
    (DIRECTORY / "imports.local.tf").write_text("\n".join(blocks), encoding="utf-8")


def snapshot() -> None:
    """Copy every current blob without stopping or changing the source container."""
    BACKUP.mkdir(parents=True, exist_ok=True)
    inventory = []
    store = BlobStore()
    try:
        for blob in store.container.list_blobs():
            payload = store.read(blob.name)
            digest = hashlib.sha256(payload).hexdigest()
            (BACKUP / f"{digest}.bin").write_bytes(payload)
            inventory.append({"name": blob.name, "sha256": digest, "bytes": len(payload)})
    finally:
        store.close()
    (BACKUP / "inventory.json").write_text(
        json.dumps({"captured_at": datetime.now(UTC).isoformat(), "blobs": inventory}, indent=2),
        encoding="utf-8",
    )


def verify_snapshot() -> int:
    inventory = json.loads((BACKUP / "inventory.json").read_text(encoding="utf-8"))["blobs"]
    store = BlobStore()
    try:
        for blob in inventory:
            local = (BACKUP / f"{blob['sha256']}.bin").read_bytes()
            remote = store.read(blob["name"])
            if hashlib.sha256(local).hexdigest() != blob["sha256"] or remote != local:
                raise ValueError(f"Blob backup verification failed: {blob['name']}")
    finally:
        store.close()
    return len(inventory)


def plan_summary(plan: dict) -> dict:
    summary = {"imports": [], "creates": [], "updates": [], "replacements": [], "deletes": []}
    allowed = {
        "docker_container.azurite",
        "docker_volume.azurite",
        "docker_network.azurite",
        "docker_container.duckdb",
        "docker_container.warehouse_permissions",
        "docker_volume.duckdb",
        "docker_volume.duckdb_backups",
        "docker_volume.dbt_artifacts",
        "docker_image.dbt",
        "docker_container.dbt",
        "docker_image.duckdb",
        "docker_image.orders_api",
        "docker_container.orders_api",
        "docker_image.airflow",
        "docker_image.airflow_postgres",
        "docker_container.airflow_init",
        "docker_container.airflow_postgres",
        'docker_container.airflow["api-server"]',
        'docker_container.airflow["scheduler"]',
        'docker_container.airflow["dag-processor"]',
        "docker_volume.airflow_postgres",
        "docker_volume.airflow_logs",
        "docker_volume.airflow_state",
        "docker_network.mailpit",
        "docker_volume.mailpit",
        "docker_image.mailpit",
        "docker_container.mailpit",
    }
    for resource in plan.get("resource_changes", []):
        address, change = resource["address"], resource["change"]
        if address not in allowed:
            raise ValueError("Unexpected resource in local-only plan")
        actions = change["actions"]
        if actions not in (
            ["no-op"],
            ["create"],
            ["update"],
            ["delete", "create"],
            ["create", "delete"],
        ):
            raise ValueError("Unsupported Terraform resource action")
        if change.get("importing"):
            summary["imports"].append(address)
        if "delete" in actions:
            if (
                not address.startswith(("docker_container.", "docker_image."))
                or "create" not in actions
            ):
                raise ValueError("Refusing plan that deletes data storage or networking")
            summary["replacements"].append(address)
        elif "create" in actions:
            summary["creates"].append(address)
        elif "update" in actions:
            summary["updates"].append(address)
    return summary


def main() -> None:
    global BACKUP
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "validate", "plan", "apply", "verify"])
    parser.add_argument("--review-directory", type=Path)
    args = parser.parse_args()
    plan_path = "migration.tfplan"
    summary_path = DIRECTORY / "migration-summary.json"
    if args.review_directory:
        review = args.review_directory.resolve()
        if not review.is_relative_to(ROOT / "artifacts") or args.action not in {
            "plan",
            "verify",
            "apply",
        }:
            parser.error(
                "Review directories must be under artifacts and used with plan/verify/apply"
            )
        review.mkdir(parents=True, exist_ok=True)
        BACKUP = review / "blobs"
        plan_path = str(review / "review.tfplan")
        summary_path = review / "plan-summary.json"
        if args.action == "plan" and (Path(plan_path).exists() or BACKUP.exists()):
            parser.error("Review plan/backup already exists; use a new review directory")
    os.chdir(ROOT)
    if args.action == "init":
        initialize_orchestration()
    env = dict(os.environ)
    key = env.get(
        "AZURITE_ACCOUNT_KEY",
        dotenv_values(ROOT / ".env", interpolate=False).get("AZURITE_ACCOUNT_KEY"),
    )
    if not key:
        parser.error("Initialize the local key: python -m retailflow.blob_storage init")
    # Avoid accidental debug logs or externally injected CLI flags leaking credentials.
    env = {
        name: value for name, value in env.items() if not name.startswith(("TF_LOG", "TF_CLI_ARGS"))
    }
    env.update(TF_VAR_azurite_account_key=key, TF_IN_AUTOMATION="1")
    local = dotenv_values(ROOT / ".env", interpolate=False)
    names = {
        # These are environment variable names, not embedded credentials.
        # pragma: allowlist nextline secret
        "airflow_db_password": "AIRFLOW_DB_PASSWORD",  # nosec B105
        # pragma: allowlist nextline secret
        "airflow_postgres_admin_password": "AIRFLOW_POSTGRES_ADMIN_PASSWORD",  # nosec B105
        # pragma: allowlist nextline secret
        "airflow_password": "RETAILFLOW_AIRFLOW_PASSWORD",  # nosec B105
        "airflow_fernet_key": "AIRFLOW_FERNET_KEY",
        # pragma: allowlist nextline secret
        "airflow_jwt_secret": "AIRFLOW_JWT_SECRET",  # nosec B105
    }
    for variable, name in names.items():
        value = env.get(name, local.get(name))
        if not value:
            parser.error("Run infrastructure init to generate local orchestration credentials")
        env[f"TF_VAR_{variable}"] = value
    env["TF_VAR_orders_interval_seconds"] = env.get(
        "RETAILFLOW_INTERVAL_SECONDS", local.get("RETAILFLOW_INTERVAL_SECONDS", "600")
    )
    env["TF_VAR_orders_seed"] = env.get("RETAILFLOW_SEED", local.get("RETAILFLOW_SEED", "42"))
    env["TF_VAR_orders_dataset_version"] = env.get(
        "RETAILFLOW_DATASET_VERSION", local.get("RETAILFLOW_DATASET_VERSION", "orders-v3")
    )
    if args.action == "init":
        print(terraform(env, "init", "-input=false", "-no-color"))
    elif args.action == "validate":
        print(terraform(env, "fmt", "-check", "-no-color"))
        print(terraform(env, "validate", "-no-color"))
    elif args.action == "verify":
        print(json.dumps({"verified_blobs": verify_snapshot()}))
    elif args.action == "plan":
        prepare_imports(env)
        # Existing installations get an application-level backup before any apply.
        if "docker_container.azurite" in (DIRECTORY / "imports.local.tf").read_text():
            snapshot()
        terraform(env, "plan", "-input=false", "-no-color", f"-out={plan_path}")
        plan = json.loads(terraform(env, "show", "-json", plan_path))
        summary = plan_summary(plan)
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
    else:
        plan = json.loads(terraform(env, "show", "-json", plan_path))
        print(json.dumps(plan_summary(plan)))
        if (BACKUP / "inventory.json").is_file():
            verify_snapshot()
        terraform(env, "apply", "-input=false", "-no-color", plan_path)
        if (BACKUP / "inventory.json").is_file():
            print(json.dumps({"verified_blobs": verify_snapshot()}))
        print("Terraform apply completed. Run plan again to check for remaining drift.")


if __name__ == "__main__":
    main()
