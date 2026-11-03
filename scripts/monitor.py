"""Read-only local pipeline monitoring with JSON alerts and meaningful exit codes."""

import argparse
import ipaddress
import json
import os
import shutil

# Fixed Docker command, no shell; captured output is never included in alerts.
import subprocess  # nosec B404
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from dotenv import dotenv_values

from retailflow.currencies import FX_QUOTES, SUPPORTED_DATASETS

ROOT = Path(__file__).resolve().parents[1]
DAGS = {"retailflow_orders": "orders", "retailflow_exchange_rates": "fx"}


def loopback_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.hostname == "localhost":
        local = True
    else:
        try:
            local = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            local = False
    if (
        parsed.scheme != "http"
        or not local
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Monitoring endpoints must be plain loopback HTTP origins")
    _ = parsed.port  # Validate the port before creating a client.
    return value.rstrip("/")


@dataclass(frozen=True)
class Config:
    orders_url: str = "http://127.0.0.1:18000"
    airflow_url: str = "http://127.0.0.1:8080"
    username: str = "admin"
    password: str = field(default="", repr=False)
    dataset_version: str = "orders-v3"
    seed: int = 42
    interval_seconds: int = 600
    orders_max_age: int = 1800
    fx_max_age: int = 108000
    run_max_age: int = 1800

    @classmethod
    def load(cls) -> "Config":
        local = dotenv_values(Path.cwd() / ".env", interpolate=False)

        def value(name: str, default: str) -> str:
            return os.environ.get(name, local.get(name) or default)

        config = cls(
            orders_url=loopback_url(value("RETAILFLOW_MONITOR_ORDERS_URL", cls.orders_url)),
            airflow_url=loopback_url(value("RETAILFLOW_MONITOR_AIRFLOW_URL", cls.airflow_url)),
            username=value("RETAILFLOW_MONITOR_AIRFLOW_USERNAME", "admin"),
            password=value("RETAILFLOW_AIRFLOW_PASSWORD", ""),
            dataset_version=value("RETAILFLOW_DATASET_VERSION", "orders-v3"),
            seed=int(value("RETAILFLOW_SEED", "42")),
            interval_seconds=int(value("RETAILFLOW_INTERVAL_SECONDS", "600")),
            orders_max_age=int(value("RETAILFLOW_MONITOR_ORDERS_MAX_AGE_SECONDS", "1800")),
            fx_max_age=int(value("RETAILFLOW_MONITOR_FX_MAX_AGE_SECONDS", "108000")),
            run_max_age=int(value("RETAILFLOW_MONITOR_RUN_MAX_AGE_SECONDS", "1800")),
        )
        if (
            config.dataset_version not in SUPPORTED_DATASETS
            or not 1 <= config.interval_seconds <= 3600
            or any(
                not 1 <= threshold <= 604800
                for threshold in (config.orders_max_age, config.fx_max_age, config.run_max_age)
            )
        ):
            raise ValueError("Invalid monitoring configuration")
        return config

    def identity(self) -> dict:
        return {
            "dataset_version": self.dataset_version,
            "seed": self.seed,
            "interval_seconds": self.interval_seconds,
        }


def source_probe(config: Config) -> dict:
    with httpx.Client(
        base_url=config.orders_url, timeout=10, trust_env=False, follow_redirects=False
    ) as client:
        response = client.get("/v1/orders", params={"limit": 1})
        response.raise_for_status()
        page = response.json()
    return {
        key: page[key]
        for key in ("latest_available_at", "dataset_version", "seed", "interval_seconds")
    }


def airflow_probe(config: Config) -> dict:
    if not config.password:
        raise ValueError("Airflow monitoring login is not configured")
    with httpx.Client(
        base_url=config.airflow_url, timeout=10, trust_env=False, follow_redirects=False
    ) as client:
        response = client.post(
            "/auth/token", json={"username": config.username, "password": config.password}
        )
        response.raise_for_status()
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]

        def get(path: str, **kwargs) -> dict:
            response = client.get(path, **kwargs)
            response.raise_for_status()
            return response.json()

        health = get("/api/v2/monitor/health")
        dags = {}
        for dag in DAGS:
            path = f"/api/v2/dags/{dag}"

            def runs(states: list[str], order: str, run_path: str = path + "/dagRuns") -> list:
                return get(
                    run_path,
                    params=[
                        *(("state", state) for state in states),
                        ("order_by", order),
                        ("limit", "1"),
                    ],
                )["dag_runs"]

            success = runs(["success"], "-end_date")
            completed = runs(["success", "failed"], "-end_date")
            active = runs(["queued", "running"], "run_after")
            dags[dag] = {
                "paused": get(path)["is_paused"],
                "last_success_at": success[0]["end_date"] if success else None,
                "latest_completed_state": completed[0]["state"] if completed else None,
                "oldest_active_at": active[0]["run_after"] if active else None,
            }
    return {
        "health": {
            key: health[key]["status"] for key in ("metadatabase", "scheduler", "dag_processor")
        },
        "dags": dags,
    }


def warehouse_probe(config: Config) -> dict:
    executable = shutil.which("docker")
    if not executable:
        raise RuntimeError("Docker CLI unavailable")
    script = Path(__file__).with_name("monitor_warehouse.py").read_text(encoding="utf-8")
    result = subprocess.run(  # nosec B603
        [
            executable,
            "exec",
            "-i",
            "retailflow-duckdb",
            "python",
            "-",
            json.dumps(config.identity()),
        ],
        input=script,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Warehouse probe unavailable")
    return json.loads(result.stdout)


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("Monitoring timestamps must have a timezone")
    return result.astimezone(UTC)


def evaluate(observations: dict, config: Config, now: datetime) -> dict:
    alerts = []

    def alert(component: str, code: str, severity: str = "warning") -> None:
        alerts.append({"component": component, "code": code, "severity": severity})

    def freshness(component: str, value: str | None, limit: int) -> None:
        if value is None:
            alert(component, "NO_SUCCESS_OR_DATA")
            return
        age = (now - timestamp(value)).total_seconds()
        if age < -60:
            alert(component, "FUTURE_TIMESTAMP", "critical")
        elif age > limit:
            alert(component, "STALE")

    for component in ("source", "airflow", "warehouse"):
        data = observations[component]
        if "error_type" in data:
            alert(component, "PROBE_UNAVAILABLE", "critical")
            continue
        try:
            if component == "source":
                if any(data[key] != value for key, value in config.identity().items()):
                    alert(component, "SOURCE_CONFIGURATION_MISMATCH", "critical")
                freshness(component, data["latest_available_at"], config.interval_seconds * 2)
            elif component == "warehouse":
                if data.get("status") == "busy":
                    alert(component, "WAREHOUSE_BUSY")
                    continue
                freshness("warehouse.orders", data["latest_order_at"], config.orders_max_age)
                for quote in FX_QUOTES:
                    published = data["fx_dates"].get(quote)
                    name = "warehouse.fx." + quote
                    if published is None:
                        alert(name, "NO_SUCCESS_OR_DATA")
                    else:
                        days = (now.date() - date.fromisoformat(published)).days
                        if days < 0:
                            alert(name, "FUTURE_TIMESTAMP", "critical")
                        elif days > 7:
                            alert(name, "STALE_RATE")
            else:
                for name in ("metadatabase", "scheduler", "dag_processor"):
                    if data["health"][name] != "healthy":
                        alert("airflow." + name, "UNHEALTHY", "critical")
                for dag, kind in DAGS.items():
                    details = data["dags"][dag]
                    if details["paused"]:
                        alert(dag, "DAG_PAUSED")
                    limit = config.orders_max_age if kind == "orders" else config.fx_max_age
                    freshness(dag, details["last_success_at"], limit)
                    if details["latest_completed_state"] == "failed":
                        alert(dag, "LATEST_RUN_FAILED", "critical")
                    if details["oldest_active_at"]:
                        age = (now - timestamp(details["oldest_active_at"])).total_seconds()
                        if age > config.run_max_age:
                            alert(dag, "RUN_STUCK")
        except (KeyError, TypeError, ValueError):
            alert(component, "INVALID_PROBE_DATA", "critical")
    exit_code = max((2 if item["severity"] == "critical" else 1 for item in alerts), default=0)
    return {
        "checked_at": now.isoformat(),
        "status": ("ok", "warning", "critical")[exit_code],
        "exit_code": exit_code,
        "alerts": alerts,
        "observations": observations,
        "thresholds": {
            "orders_seconds": config.orders_max_age,
            "fx_pipeline_seconds": config.fx_max_age,
            "active_run_seconds": config.run_max_age,
            "source_seconds": config.interval_seconds * 2,
            "fx_publication_days": 7,
        },
    }


def check(config: Config) -> dict:
    observations = {}
    for name, probe in (
        ("source", source_probe),
        ("airflow", airflow_probe),
        ("warehouse", warehouse_probe),
    ):
        try:
            observations[name] = probe(config)
        except Exception as error:  # Each unavailable service must become an alert, not silence.
            observations[name] = {"error_type": type(error).__name__}
    return evaluate(observations, config, datetime.now(UTC))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-save", action="store_true", help="Print JSON without saving a report")
    args = parser.parse_args()
    try:
        report = check(Config.load())
    except (ValueError, TypeError):
        report = {
            "status": "critical",
            "exit_code": 2,
            "alerts": [
                {
                    "component": "configuration",
                    "code": "INVALID_CONFIGURATION",
                    "severity": "critical",
                }
            ],
        }
    if not args.no_save:
        directory = ROOT / "artifacts/monitoring"
        directory.mkdir(parents=True, exist_ok=True)
        identity = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex
        path = directory / (identity + ".json")
        report["report_path"] = str(path)
        with path.open("x", encoding="utf-8") as output:
            output.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    raise SystemExit(report["exit_code"])


if __name__ == "__main__":
    main()
