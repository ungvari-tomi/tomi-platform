# Local freshness and failure monitoring

Status: implemented locally; focused tests and a live read-only check passed.
No monitor schedule, external notification channel or continuous alert delivery
has been configured. The command reports when you run it; it does not page you.
It needs the existing Python environment, Docker access and running local services.
No new dependency, container, image rebuild, paid service or Terraform apply is needed.

```powershell
.\.venv\Scripts\python.exe -m scripts.monitor
$LASTEXITCODE
```

JSON is printed and saved to a new ignored `artifacts/monitoring/<id>.json` file.
Use `--no-save` to print only. Exit 0 means all checked signals passed, 1 means
warnings need review, and 2 means a critical failure, unavailable probe or invalid
configuration. A missing service or a busy warehouse never becomes a healthy check.
Reports include effective thresholds and aggregate observations, not order records.

## Checked signals

| Signal | Default rule |
| --- | --- |
| Orders API | Latest advertised source timestamp within twice its configured generation interval; dataset version/seed/interval match local configuration |
| Warehouse orders | Current dataset version, seed and interval have an order at most 30 minutes old |
| Orders DAG | A successful run ended within 30 minutes |
| FX DAG | A successful run ended within 30 hours |
| Published ECB FX dates | All nine foreign currencies exist, dated today or up to seven days earlier |
| Run failures | Most recently completed run failed, even when an earlier successful run is fresh |
| Queued/running work | Oldest active run's run-after timestamp exceeds 30 minutes |
| Operational state | Both DAGs enabled; Airflow metadata, scheduler and DAG processor healthy |

FX rates and FX ingestion have different clocks. Friday's rates can remain valid
on Monday while the daily pipeline still needs to run. The seven-day allowance
matches EUR valuation rules; the monitor does not independently consult the ECB
holiday calendar. It does not validate historical FX coverage for every order,
backup age, every individual task attempt, blob contents or container vulnerabilities.
Those controls remain in their existing loaders, tests, deployment checks or future work.

Successful runs are queried separately from failed/active runs, so a page of recent
failures cannot hide an older success. Failure alerts clear after a newer completed
run succeeds; old historical failures do not alert forever. A queued/running job
is allowed time to finish; temporary retries do not count as a completed failure.
Future instant timestamps allow 60 seconds of clock skew; publication dates may
not be in the future. Malformed/naive timestamps produce a critical alert.

## Configuration

Process environment overrides working-directory `.env`, then defaults. The monitor
reads configuration on each invocation. `.env.example` lists optional settings:

| Variable | Default |
| --- | --- |
| RETAILFLOW_MONITOR_ORDERS_URL | http://127.0.0.1:18000 |
| RETAILFLOW_MONITOR_AIRFLOW_URL | http://127.0.0.1:8080 |
| RETAILFLOW_MONITOR_AIRFLOW_USERNAME | admin |
| RETAILFLOW_MONITOR_ORDERS_MAX_AGE_SECONDS | 1800 |
| RETAILFLOW_MONITOR_FX_MAX_AGE_SECONDS | 108000 |
| RETAILFLOW_MONITOR_RUN_MAX_AGE_SECONDS | 1800 |

Airflow authentication reuses `RETAILFLOW_AIRFLOW_PASSWORD` from the process or
ignored .env. Dataset identity uses existing RETAILFLOW_DATASET_VERSION, SEED and
INTERVAL_SECONDS settings. Restart the API/managed runtime to apply changes to
their configuration; the monitor will flag a mismatch until they agree. Adjust
the orders freshness window if you choose a slower generation/scheduling interval.
Thresholds must be whole seconds from 1 through 604800.

Run from the project root to use its .env. No credentials are generated or changed.
The configured user is the existing local admin; this command does not establish
a new read-only RBAC role. Its code uses authentication followed by read-only
Airflow endpoints and never patches DAGs, triggers runs, retries tasks or writes data.

## Probe and reporting boundaries

HTTP endpoints must be plain loopback HTTP origins without embedded credentials,
paths, query strings or fragments. Redirect following and environment proxies are
disabled so authentication stays local. Localhost/Docker and the local operator
remain trusted; this is not a defense against an already compromised workstation.
Only exception types are reported on probe failure; response bodies, passwords,
tokens, full Docker inspection results and environment dumps are never emitted.

The host sends the small `scripts/monitor_warehouse.py` probe over Docker stdin to
the existing warehouse container. It opens the existing database read-only with
external access disabled under the same 0660 FileLock as loaders and backups.
It never initializes a database. A writer holding the lock beyond five seconds
produces WAREHOUSE_BUSY (warning/exit 1), since freshness could not be verified.
No Docker socket is mounted into Airflow, and no monitor runs inside its scheduler.

Reports are owner-operated internal operational metadata: dataset identity,
health/pause states, aggregate timestamps, currency dates and alert codes. Lineage
is the local API, Airflow metadata and raw warehouse tables. Approved use is local
troubleshooting and future explicitly configured alert automation; reports are
not audit/compliance evidence or a freshness SLA. Files are excluded from Git and
delivery. They have no automatic retention/deletion policy; the owner may remove
obsolete reports explicitly. Do not upload them to public artifacts automatically.

## Verification and next milestone

Focused tests cover healthy/weekend data, stale/missing/future dates, source
identity mismatch, paused/failed/stuck runs, unhealthy services, environment
precedence, redirect rejection, secret suppression, exit codes and separate report
files. Disposable DuckDB tests verify current-identity filtering, unchanged database
bytes, existing-writer lock enforcement and refusal to create a missing database.
31 monitoring tests passed; related CI-selection tests passed during development.
The real local invocation returned OK with fresh orders, both DAGs enabled/healthy
and all nine FX currencies present. No live schedules/data were altered to test failures.

The [local Mailpit inbox](local-email.md) and email delivery command are now prepared
as the next milestone; its saved infrastructure plan awaits approval. This replaces
the proposed Gmail route: no provider signup, mailbox or credentials are required.
Scheduling this check still requires its own reviewed milestone.
Recommended next step after publishing this command: choose a local notification
channel and a Terraform-managed monitoring schedule that does not depend on the
Airflow service it monitors being healthy.
