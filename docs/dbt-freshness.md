# dbt freshness and failure-event email

Status: deployed locally on 2026-11-04 from source commit 061e068 after owner
approval of the saved plan. Both live pipelines passed freshness and build checks.
No live data was corrupted or failure notification manufactured.

## Gate and contracts

The existing dbt_build task invokes the shared wrapper's `pipeline` action:

1. `dbt source freshness` checks current orders (30-minute error threshold) and
   the FX ingestion ledger (30-hour error threshold).
2. `dbt test --select tag:freshness --vars '{"check_freshness": true}'` checks
   future orders and all nine foreign-currency publication/load dates.
3. `dbt build` runs the existing three models and 37 data tests.

The entire sequence holds the common 0660 warehouse FileLock. A nonzero command
stops later commands and fails the task. The task keeps its existing retry policy;
Airflow's DAG-level failure callback runs only when the DAG reaches failed state.
There are no new tasks or schedules and no reminder/recovery emails.

Orders are filtered by dataset version, seed and generation interval through
ops.datasets. Fresh legacy data cannot mask an empty/stale active dataset. Every
required FX currency must have a matching verified membership/ledger load within
30 hours and a publication date between today and seven calendar days earlier.
Friday rates can remain valid on Monday while ingestion continues daily. A fresh
load for one currency cannot mask a stale/missing currency. Future data fails;
instant timestamps allow 60 seconds of skew. Raw history is preserved.

The limits are explicit dbt source/test contracts, not Python alert thresholds.
The seven-day publication rule matches the valuation model; no independent ECB
holiday calendar or freshness SLA is claimed. Freshness runs at the existing DAG
schedules, so a stopped/paused scheduler does not generate a dbt failure event.

The pinned dbt Core 1.11 runtime uses its supported
[source freshness command](https://docs.getdbt.com/reference/commands/source).
As in dbt generally, ordinary build does not implicitly run source freshness.
Tagged freshness tests are enabled only for the wrapper freshness/pipeline modes,
allowing historical model development without wall-clock failures.

## Local commands

```powershell
.\scripts\dbt-dev.ps1 freshness
.\scripts\dbt-dev.ps1 pipeline
```

The developer command still uses the shared managed warehouse under the wrapper
lock, mounts the project read-only and preserves artifacts locally. Override
DatasetVersion, Seed and IntervalSeconds when the active API identity differs
from orders-v3 / 42 / 600. Scheduled and standalone managed environments receive
those values from Terraform. No authentication values enter the isolated dbt child.

Artifacts retain independent source_freshness/target/sources.json,
freshness_tests/target/run_results.json and final target/run_results.json inside
each existing UUID invocation. The task checks two passing source checks, two
passing freshness tests and successful final build records. Missing/empty evidence
fails closed. Each command has a 120-second timeout; the parent permits 450 seconds
including locking and retains the existing 20-minute task execution limit.

## Failure delivery

Both DAGs register retailflow.alerts.dag_failure at DAG level, with no task retry
or success callbacks. The processor connects to Mailpit over its approved Docker
network; the scheduler and API server do not join that inbox network. Email contains
the allowlisted DAG name, failure state, observation time and a hashed run identity,
never raw run IDs, exception text, SQL, credentials or source rows.

A per-event FileLock and receipt in /opt/airflow/logs/failure-email serialize
duplicate callback handling. Receipts are published after SMTP acceptance. Mailpit
also ignores duplicate Message-IDs. Each new failed DAG run can generate one email;
replaying the same run does not create reminders. Old polling mail-state.json is
retained but unused. Explicit diagnostic test emails remain available.

Airflow documents that callbacks run on execution-driven state changes, while
UI/CLI state edits do not trigger them, and callback errors appear in processor
logs ([callback behavior](https://airflow.apache.org/docs/apache-airflow/3.3.2/administration-and-deployment/logging-monitoring/callbacks.html)).
SMTP errors raise a sanitized visible callback error; Airflow does not guarantee
callback retry. This is best-effort notification with deduplication, not a durable
outbox or exactly-once transport. The failed DAG remains failed if email is unavailable.

## Evidence and rollout

Real dbt tests passed nine disposable scenarios: fresh data/three-day FX rates,
stale current orders despite fresh legacy data, empty active orders, stale global
FX ingestion, one stale currency load, missing currency, old publication, future
publication and future orders. Source tables were compared before/after to ensure
the checks did not alter them. A partial-FX scenario passed the global source
freshness check but correctly failed the per-pair test.

The real Airflow task passed freshness/build and propagated deliberate quality
corruption in disposable storage. The registered callback was invoked from an
isolated Airflow image over Docker DNS into disposable Mailpit: two failed runs
produced two emails, a duplicate callback was suppressed, and success/running
events produced none. This tests registered callback invocation and transport;
actual scheduler-driven terminal callback dispatch has not been demonstrated by
manufacturing a live failure. No deliberate bad-data live test is authorized.

The approved local plan replaced three images and six containers (Airflow services
and initializer, standalone dbt, Mailpit), with no volume/network deletion or orders
API replacement. Its initial review verified 882 blobs. Deployment paused/drained
both DAGs and took a fresh locked warehouse snapshot plus a 1,092-blob backup.
All seven warehouse table schemas/row checksums matched after apply; the existing
inbox message and protected services were preserved. Only the processor joined
the Mailpit network. The post-apply Terraform plan reported no changes.

Controlled run dbt_freshness_deployment_20261104T061102Z succeeded in both DAGs:
all six tasks passed on attempt 1; each pipeline passed two source-freshness checks,
two freshness tests, three models and 37 existing data tests. Independent final
snapshot checks preserved every historical raw, membership and ledger row, with
orders increasing from 569 to 570 and FX records remaining 19. Both schedules are
enabled. Successful runs generated no emails, and all 1,092 backed-up blobs still
matched afterward. Full hosted CI passed for the source commit. Deployment evidence
is retained under ignored artifacts/deployment-dbt-freshness-20261104/deployment.json.
No paid service or cloud infrastructure was provisioned.
