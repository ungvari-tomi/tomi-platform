"""Exact arithmetic and temporal coverage checks against the real dbt model."""

import json
import os
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest

from retailflow.currencies import COUNTRY_PROFILES
from retailflow.exchange_rates import huf_minor_to_eur_minor
from retailflow.warehouse import DDL

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_DBT_IMAGE") != "1", reason="Real dbt image required"
)
ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def test_eur_model_exact_rounding_date_matching_and_visible_gaps(tmp_path: Path):
    fixture_input = tmp_path / "input"
    fixture_input.mkdir(mode=0o755)
    database = fixture_input / "retailflow.duckdb"
    with duckdb.connect(str(database)) as c:
        c.execute(DDL)
        orders = [
            (1, "2026-10-30T12:00:00Z", "HU", "HUF", 200200),
            (2, "2026-10-30T12:00:00Z", "DE", "EUR", 505),
            (3, "2026-11-01T12:00:00Z", "HU", "HUF", 200200),
            (4, "2026-10-29T12:00:00Z", "HU", "HUF", 200200),
            (5, "2026-10-22T12:00:00Z", "HU", "HUF", 200200),
            (6, "2026-10-21T12:00:00Z", "HU", "HUF", 200200),
            (7, "2026-11-02T00:30:00+02:00", "HU", "HUF", 200200),
        ]
        quote_rates = {
            "GBP": "0.8",
            "CHF": "0.9",
            "PLN": "4.2",
            "CZK": "25",
            "RON": "5",
            "SEK": "11",
            "NOK": "12",
            "DKK": "7.5",
        }
        for country, (currency, minimum, _, _) in COUNTRY_PROFILES.items():
            if currency in quote_rates:
                orders.append((len(orders) + 1, "2026-10-30T12:00:00Z", country, currency, minimum))
        for identifier, created, country, currency, total in orders:
            c.execute(
                "insert into raw.orders values ('dataset',?,?,?,?,'p',1,?,?,'m')",
                [identifier, created, country, currency, total, total],
            )
        rates = [
            ("HUF", "2026-10-14", "400"),
            ("HUF", "2026-10-28", "380"),
            ("HUF", "2026-10-30", "400.000000000001"),
            ("HUF", "2026-11-02", "100"),
        ]
        rates += [(quote, "2026-10-30", rate) for quote, rate in quote_rates.items()]
        for quote, published, rate in rates:
            c.execute(
                "insert into raw.exchange_rates values ('ECB','EUR',?,?,?,'rate-manifest')",
                [quote, published, Decimal(rate)],
            )
        requests = [
            ("HUF", "2026-10-30", "2026-10-30"),
            ("HUF", "2026-11-01", "2026-10-30"),
            ("HUF", "2026-11-01", "2026-11-02"),  # Deliberately future-only candidate.
            ("HUF", "2026-10-21", "2026-10-14"),
            ("HUF", "2026-10-22", "2026-10-14"),
        ]
        requests += [(quote, "2026-10-30", "2026-10-30") for quote in quote_rates]
        requests += [requests[0]]  # Duplicate requests must not duplicate orders.
        for i, (quote, requested, published) in enumerate(requests):
            c.execute(
                "insert into ops.fx_batch_records values (?,'ECB','EUR',?,?,?)",
                [f"request-{i}", quote, published, requested],
            )
    owner = "retailflow-eur-test-" + uuid4().hex

    def docker(*args):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    try:
        docker(
            "run",
            "--name",
            owner,
            "--network",
            "none",
            "--mount",
            "type=volume,destination=/warehouse",
            "--mount",
            f"type=bind,source={fixture_input},target=/input,readonly",
            "--entrypoint",
            "python",
            "retailflow-dbt:validation",
            "-c",
            "import shutil; "
            "shutil.copyfile('/input/retailflow.duckdb','/warehouse/retailflow.duckdb')",
        )
        mounts = json.loads(docker("inspect", owner, "--format", "{{json .Mounts}}"))
        volume = next(m["Name"] for m in mounts if m["Destination"] == "/warehouse")
        result = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts/dbt-dev.ps1"),
                "build",
                "-WarehouseVolume",
                volume,
                "-Image",
                "retailflow-dbt:validation",
                "-ArtifactDirectory",
                str(tmp_path / "artifacts"),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        exported = docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--volumes-from",
            owner,
            "--entrypoint",
            "python",
            "retailflow-dbt:validation",
            "-c",
            "import duckdb,json; c=duckdb.connect('/warehouse/retailflow.duckdb'); "
            "print(json.dumps(c.execute('select order_id,order_date,conversion_status,"
            "eur_total_minor,total_eur from staging.orders_eur order by order_id')"
            ".fetchall(),default=str))",
        )
        rows = {r[0]: r for r in json.loads(exported)}
        assert len(rows) == len(orders)
        assert rows[1][3] == 500  # Binary floating-point would incorrectly round this near-tie up.
        assert rows[2][3:] == [505, "5.05"]
        assert rows[3][3] == 500
        assert rows[4][2:] == ["missing_rate", None, None]
        assert rows[5][2:] == ["stale_rate", None, None]
        assert rows[6][3] == 501  # Exactly half a cent rounds UP, not to an even number.
        assert rows[7][1:4] == ["2026-11-01", "converted", 500]  # UTC date wins.
        for identifier, _, _, currency, total in orders[7:]:
            assert rows[identifier][3] == huf_minor_to_eur_minor(
                total, Decimal(quote_rates[currency])
            )
    finally:
        subprocess.run(["docker", "rm", "--volumes", owner], capture_output=True, check=False)
