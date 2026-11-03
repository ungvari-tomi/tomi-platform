# RetailFlow

[Single-maintainer CI and delivery](docs/ci-cd.md) passed hosted validation and delivery checks.
The local release-review command combines package verification, blob backup and saved-plan review.

[Ten-currency generation and EUR valuation](docs/european-currencies.md) are deployed; the versioned migration and live pipelines are verified.

[Airflow dbt automation](docs/airflow-dbt.md) is deployed; both live three-task verification runs passed.

Browse an independent DuckDB snapshot with [DBeaver using the export helper](docs/database-viewing.md).

Edit dbt SQL locally, then run `.\scripts\dbt-dev.ps1 build`; see the [development workflow](docs/dbt-runtime.md#develop-models-from-vs-code).

A [Terraform-managed dbt Core runtime](docs/dbt-runtime.md) is deployed; its managed source checks passed.

[Daily warehouse backups and recovery](docs/warehouse-backup.md) are deployed; startup backup and separate-file restore verification passed.

A local-first, production-oriented retail data engineering portfolio project.
Current implementation: a ten-currency synthetic orders API, freshness checker, and a separate
client for nine real ECB EUR-base reference pairs via Frankfurter, and Azurite landing
storage for CSV orders and JSON exchange rates.
A transactional raw DuckDB loader is deployed through Terraform, with shared-volume
Airflow access and checksum-verified batch lineage. dbt runtime checks are deployed; initial [staging models](docs/staging-models.md)
and all 29 tests have passed in the managed warehouse. Dashboards and cloud deployment
remain planned. This is not a production-deployed system.

A **Terraform-owned DuckDB runtime** is running: a local image, non-root
container, and protected database volume. Airflow loads verified landed batches
into its raw tables using a shared writer lock. See
[DuckDB runtime setup and the reviewed plan](docs/duckdb-runtime.md).

**Terraform-managed Airflow orchestration is running, with both DAGs enabled.** It
includes a containerized orders API, PostgreSQL metadata database, and DAGs for
orders every ten minutes and daily exchange rates. The 15-resource deployment
preserved existing Azurite data; the follow-up Terraform plan has no changes. See
[Airflow setup, activation, and validation](docs/orchestration.md).

Open Airflow at <http://127.0.0.1:8080> (user `admin`; password is
`RETAILFLOW_AIRFLOW_PASSWORD` in your ignored `.env`). The managed source API is
at <http://127.0.0.1:18000/docs>. Scheduled and manual extraction runs have succeeded;
verified CSV/JSON batches are in Azurite. The downstream `load` tasks and
transactional raw tables are described in [warehouse loading](docs/warehouse-loading.md).

## Run the synthetic API on Windows

Open this folder in VS Code and select `.venv\Scripts\python.exe` using
**Python: Select Interpreter**. See [environment setup](docs/environment.md) for
dependency installation. No account, credentials, Docker, or paid services are needed.

In the VS Code PowerShell terminal:

```powershell
.\.venv\Scripts\python.exe -m uvicorn retailflow.api:app --host 127.0.0.1 --port 8000
```

Keep that terminal running. Open a second terminal to make calls:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
$first = Invoke-RestMethod 'http://127.0.0.1:8000/v1/orders?limit=3'
$first.orders | Format-Table
```

The default interval is **ten minutes**. For the quick test below, stop the API
and restart it with a five-second
interval in its terminal:

```powershell
$env:RETAILFLOW_INTERVAL_SECONDS = '5'
.\.venv\Scripts\python.exe -m uvicorn retailflow.api:app --host 127.0.0.1 --port 8000
```

Or run the automated check, which makes two real HTTP calls, waits just over one
generation interval, checks timestamp freshness against the client clock, and
requires new IDs and an advancing order timestamp:

```powershell
.\.venv\Scripts\python.exe -m retailflow.check_freshness
```

It prints JSON with `status: passed`, the two latest IDs, and the number of new
orders, or exits unsuccessfully if validation fails. Stop the API with **Ctrl+C**.
After testing, set `$env:RETAILFLOW_INTERVAL_SECONDS = '600'` and restart the API
to restore ten-minute generation. The checker refuses waits over 30 seconds by
default; use `--max-wait-seconds 601` to deliberately test the ten-minute interval.
Interactive API documentation is available at <http://127.0.0.1:8000/docs>.

## How data stays fresh

One order becomes available every ten minutes by default. The service computes records on
request using UTC slots starting at 2025-01-01, a seed, and a versioned SHA-256
mapping. It does not run a background producer or persist orders. Repeated requests
within a slot return the same records; crossing a slot adds a new ID and timestamp.
Earlier slots can be replayed with `after_id=0`. Restarting with the same settings
preserves order identities and content. This simulates a live source, rather than
proving that an external upstream system is healthy.

- `GET /health`: process liveness only, not a freshness assertion.
- `GET /v1/orders`: latest 20 orders by default; `limit` accepts 1–100.
- `after_id=N`: ascending records strictly after N. Follow `next_after_id` while
  `has_more` is true. An up-to-date or future cursor returns an empty list.
- Responses include UTC `observed_at`, `latest_available_at`, source age,
  dataset version, seed, generation interval, and a `Cache-Control: no-store` header.
- Prices and totals are integer minor currency units. HUF and EUR are separate;
  the orders API does not convert or sum different currencies.

Dataset `orders-v2` uses illustrative prices of **EUR 5–200** and
**HUF 2,000–80,000** per unit. EUR prices are stored as 500–20000 minor units;
HUF prices as 200000–8000000 minor units, in multiples of 100 (whole forints).
These are synthetic retail ranges, not prices derived from exchange rates.
The endpoint remains `/v1/orders`: the response schema is unchanged, while the
dataset content version changed. Reset cursors and replay existing samples when
switching from `orders-v1` to `orders-v2`, since earlier order prices change.

Local configuration is loaded from `.env` in the current working directory when
the API starts. Run commands from the project root. On a fresh clone, create it:

```powershell
Copy-Item .env.example .env
```

Edit `.env` to set the generation interval (`5` for testing, `600` for ten minutes),
then restart the API. `.env` is ignored by Git; `.env.example` is the shared template.

PowerShell/process environment variables take priority over `.env`, followed by
built-in defaults. You can still override settings before starting the API:

```powershell
$env:RETAILFLOW_SEED = '42'
$env:RETAILFLOW_INTERVAL_SECONDS = '600'
```

To use the `.env` interval after a PowerShell override, clear the override in the
API terminal with `Remove-Item Env:RETAILFLOW_INTERVAL_SECONDS -ErrorAction SilentlyContinue`
and restart. Only `.env` in the working directory is read; parent directories are
not searched, and variable interpolation is disabled. The file is not modified.

The interval must be 1–3600 seconds. Invalid configured values fail at startup.
Changing seed or interval changes the dataset: reset any
consumer cursor. For quick testing use interval `5`; the checker waits
the configured interval plus 0.25 seconds. Keep client and server clocks accurate.

## Get real exchange rates

This client calls Frankfurter directly; it does not require the local API to run:

```powershell
.\.venv\Scripts\python.exe -m retailflow.exchange_rates --date 2026-10-31
```

Verified output from a live request on 2026-10-31:

```json
{"requested_date":"2026-10-31","rate_date":"2026-10-30","base":"EUR","quote":"HUF","rate":"366.33","source":"Frankfurter","provider":"ECB"}
```

The convention is **1 EUR = rate HUF**. A weekend request can return an earlier
publication date; both dates are retained. There is no API key or subscription.
Internet access is required for live rates, but the orders API and automated
tests remain offline-capable. There is no local exchange-rate endpoint or warehouse
rate table yet. Responses can be landed in Azurite. See
[exchange-rate behavior and provenance](docs/exchange-rates.md).

The tested `huf_minor_to_eur_minor` helper uses Decimal arithmetic and rounds
half up to euro cents. At an illustrative 400 HUF/EUR, 800000 HUF minor units
(8,000 HUF) convert to 2000 euro cents (EUR 20). Convert an entire order total
once; multiplying rounded converted unit prices can produce a different total.
This helper is not yet connected to order responses or a transformation layer.

## Land CSV and JSON in Azure-compatible local storage

Azurite is managed by **local Terraform Community CLI**. The migration preserved
the existing volume and network, verified the backed-up blobs, and finished with
a no-change Terraform plan. Compose has been retired. See
[Terraform setup and migration](docs/terraform.md).

Azurite now provides the landing layer, before any future DuckDB loading:

```text
Orders API → validated CSV + metadata ┐
                                    ├→ Azurite Blob Storage → DuckDB (planned)
Frankfurter → original JSON + metadata┘
```

With Docker Desktop running, initialize the local key and start storage:

```powershell
.\.venv\Scripts\python.exe -m retailflow.blob_storage init
.\.venv\Scripts\python.exe -m retailflow.infrastructure init
.\.venv\Scripts\python.exe -m retailflow.infrastructure plan
# Review the printed summary before applying:
.\.venv\Scripts\python.exe -m retailflow.infrastructure apply
.\.venv\Scripts\python.exe -m retailflow.blob_storage check
```

Keep the orders API running in another terminal. Save one page of orders and one
date-specific exchange-rate response:

```powershell
$ordersBatch = .\.venv\Scripts\python.exe -m retailflow.landing orders --limit 5 | ConvertFrom-Json
$ratesBatch = .\.venv\Scripts\python.exe -m retailflow.landing exchange-rates --date 2026-10-31 | ConvertFrom-Json
.\.venv\Scripts\python.exe -m retailflow.blob_storage list
```

Download the landed data, verifying its checksum before creating a local file:

```powershell
.\.venv\Scripts\python.exe -m retailflow.landing download --manifest $ordersBatch.manifest --output artifacts/orders.csv
.\.venv\Scripts\python.exe -m retailflow.landing download --manifest $ratesBatch.manifest --output artifacts/rates.json
Import-Csv artifacts/orders.csv | Format-Table
Get-Content artifacts/rates.json
```

Choose different output filenames if they already exist. Source blobs live in the
`landing` container; paths include source, UTC ingestion date, and a unique batch
ID. Orders CSV is explicitly a converted representation of the API's JSON.
Frankfurter JSON preserves the response body without reserialization. Each has a
`metadata.json` sidecar with request URL/parameters, retrieval time, schema/version
or rate dates, byte count, and SHA-256. Metadata is written last as the batch
completion marker. See [storage behavior, tests, and limitations](docs/landing-storage.md).

## Validation and boundaries

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check retailflow tests
.\.venv\Scripts\ruff.exe format --check retailflow tests
.\.venv\Scripts\python.exe -m bandit -r retailflow
```

Tests verify reproducibility, pagination, business rules, invalid inputs,
advancing source time, and stale-data rejection using an injected clock. The real
HTTP checker separately verifies the running service. Schema validation uses Pydantic.

Verified on 2026-10-31: 43 pytest cases passed, including realistic currency prices,
date fallback, bounded retry behavior, invalid responses, and decimal conversion.
Ruff lint/format and Bandit passed. The earlier API-only source/configuration secret
scan found no findings (without credential verification or history scanning).
The earlier live HTTP checker observed
IDs 11550938 then 11550939 at the five-second interval. A subsequent live request
confirmed the 600-second default and a fresh order timestamp; the checker also
rejected its ten-minute wait as intended. The ten-minute boundary was tested
with a controlled clock, rather than waiting ten minutes in the live check.

All order records are synthetic and contain no customer personal information. The
project maintainer owns this internal synthetic demonstration dataset; approved
use is development/testing. Landed data persists in a Docker volume; automatic
retention and deletion are not implemented. There is no persistent access audit.
The orders API has no authentication, TLS, rate limiting,
or durable storage. Run it on loopback only; do not expose it publicly. Schema
versioning is present but schema evolution and incremental ingestion are not yet
implemented. No cloud resources are provisioned. Local execution is free apart
from your own hardware, electricity, and network costs.

If startup fails, check the environment variables and whether port 8000 is occupied.
Use `--port 8001` and checker `--url http://127.0.0.1:8001` to select another port.
If the checker reports stale data, confirm that the intended process is running,
the clocks are correct, and the API settings have not changed between calls.

MIT licensed; see [LICENSE](LICENSE).
