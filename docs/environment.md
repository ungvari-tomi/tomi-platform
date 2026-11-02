# Development environment

A [Terraform-managed dbt Core runtime](dbt-runtime.md) is deployed; its managed source checks passed.

The deployed [warehouse loader](warehouse-loading.md) adds explicit `pytz` support
for DuckDB timestamp conversion. Sync the latest lockfile before running it. The
shared-volume loading/image update was applied on 2026-11-02.

Environment setup, the synthetic source API, and a standalone exchange-rate HTTP
client and Azurite landing ingestion are implemented. Transactional raw warehouse loading is implemented. See
[README](../README.md) for API and exchange-rate commands. Unit tests use saved
rate responses; only the exchange-rate CLI needs internet access for live data.
Blob landing needs local Azurite; its opt-in integration tests need Docker running.
Local Terraform 1.17.1 and Docker provider 4.6.0 now manage Azurite; Compose has
been retired. See [the Terraform runbook](terraform.md) for infrastructure commands.
No HCP Terraform account is required.
Terraform now also manages the deployed [DuckDB runtime](duckdb-runtime.md) and
[Airflow orchestration](orchestration.md), including PostgreSQL and a containerized
API. The combined plan was applied; both extraction DAGs are enabled and their
first end-to-end extraction runs have been verified. Airflow dependencies
stay in its Docker image, so installing Airflow into the Windows virtualenv is not required.
Use `uv sync --locked` to install the added Azure Blob Storage SDK before running
the [landing commands](landing-storage.md). Image downloads need internet access once.
Python 3.12 is intentionally required; widening support needs separate verification.
`uv.lock` pins direct and transitive dependencies. Review lockfile changes before upgrades.
uv 0.13.6 was used for this setup.

## Windows and VS Code: current workspace

Open the repository folder in VS Code and select **Python: Select Interpreter**
from the command palette, then choose `.venv\Scripts\python.exe`.
Activation is optional; these PowerShell commands avoid execution-policy changes:

```powershell
$env:UV_PYTHON_INSTALL_DIR = Join-Path (Get-Location) '.python-runtime'
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
& .\.bootstrap\packages\bin\uv.exe sync --locked
& .\.venv\Scripts\python.exe --version
& .\.bootstrap\packages\bin\uv.exe pip check
& .\.venv\Scripts\ruff.exe check .
& .\.venv\Scripts\ruff.exe format --check .
& .\.venv\Scripts\python.exe -m pip_audit --cache-dir .uv-cache\pip-audit
```

`uv sync` and vulnerability auditing need internet access when data is not cached.
Neither needs cloud accounts, credentials, subscriptions, or a credit card.
Runtime, environment, and caches are ignored by Git. Existing system Python is unchanged.

The API loads `.env` from the working directory at startup. A local `.env` has
been created with the ten-minute default; on a fresh clone copy `.env.example`
to `.env`. Restart the API after changes. Process environment variables override
file values. See the README for commands to clear a previous PowerShell override.

## Fresh clone

With uv 0.13.6 already installed, run these commands from the repository root:

```powershell
$env:UV_PYTHON_INSTALL_DIR = Join-Path (Get-Location) '.python-runtime'
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
uv python install 3.12 --no-bin
uv sync --locked
```

Then use the `.venv` interpreter as described above. Bootstrap uv is a local tool,
not a committed executable. If the old system Python cannot create a virtual
environment, uv can be downloaded into the workspace using working system pip:

```powershell
python -m pip install --index-url https://pypi.org/simple --target .bootstrap\packages uv==0.13.6
```

## Verification on 2026-10-31

- Python 3.12.16 executed successfully.
- API, HTTP client, validation, server, DuckDB, and test/security tool imports succeeded.
- An in-memory FastAPI health route returned HTTP 200 with the expected JSON.
- A DuckDB in-memory SQL aggregation returned the expected total of 30.
- Dependency consistency check: all 53 installed packages compatible.
- Offline lockfile consistency check passed.
- Ruff lint and format checks passed; no application code exists yet.
- pip-audit reported no known vulnerabilities for the installed environment.

The results above record the initial environment setup, before application code
was added. The API now has a pytest suite and a real HTTP freshness checker; use
the README commands to verify the current code. No CI, containers, cloud services,
or production access controls have been implemented or validated.

The initial FastAPI TestClient smoke check emitted a Starlette deprecation warning
for HTTPX. The implemented API tests use HTTPX's ASGI transport instead.
