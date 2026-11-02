# Transactional raw warehouse loading

Status: implemented, tested, and deployed locally through Terraform on 2026-11-02.
Both running DAGs include extraction and downstream raw loading. Persistent volumes
were preserved; the post-apply Terraform plan has no changes.

## Data flow and ownership

Both existing DAGs run `extract → load`. Extraction keeps
landing orders as CSV and FX responses as original JSON in Azurite. The downstream
task lists completed metadata manifests for its source and loads previously unseen
batches, including older manually landed batches. It never rereads source APIs to
reconstruct stored data. Airflow retries a failed load independently of extraction.

Python owns schema version 1 and transactions. Terraform continues to own the
runtime images/containers, network, resource limits, and persistent database volume.
No new SQL server, Docker socket mount, or paid service is introduced. The shared
volume approach keeps the analytical file local; it is not a multi-host warehouse.

## Schema, contracts, and replay

| Table | Responsibility |
|---|---|
| `raw.orders` | Typed, deduplicated orders keyed by dataset identity and order ID |
| `raw.exchange_rates` | Exact `DECIMAL(24,12)` rates keyed by provider, pair, publication date |
| `ops.datasets` | Dataset version, seed, and generation interval |
| `ops.loaded_batches` | Manifest/payload checksums, retrieval/load times, counts, original metadata |
| `ops.order_batch_records` | Every order-to-batch membership, including overlapping batches |
| `ops.fx_batch_records` | FX batch membership and requested date, including weekend requests |
| `ops.schema_version` | Explicit initial warehouse schema version |

The loader validates manifest version, source, payload path, encoding, byte length,
SHA-256, and timezone-aware retrieval timestamps before opening a write transaction.
CSV has fixed columns, integer numeric fields, valid timestamps/countries/currencies,
v2 currency-specific price ranges, positive quantities, matching totals, unique IDs
within a batch, and a matching manifest row count. Unknown columns fail explicitly.
FX must be the supported ECB EUR/HUF observation, positive/finite with supported
decimal precision, and have matching payload/metadata dates and rates.

Orders from different seeds/intervals/versions have distinct dataset IDs. Repeated
identical records in different batches reuse canonical rows while preserving each
batch membership. Weekend requests can reuse Friday's reference rate while retaining
their separate requested dates. A conflicting historical order or revised rate
fails explicitly; automatic correction/revision modeling is not implemented.

Raw inserts, provenance memberships, and the loaded-batch ledger commit in one
transaction. Failure rolls back the complete batch. An unchanged loaded manifest
is replayed without inserting again; changing a previously loaded manifest fails.
The loader also rechecks payload integrity on replay. Separate batches commit
independently, so earlier valid batches remain committed if a later one fails.
Incomplete uploads without metadata are ignored. Invalid complete batches fail
the task; automated quarantine/skip policies are future work.

No cross-schema SQL foreign keys are claimed. Provenance memberships are written
by the loader in the same transaction as records and ledger entries; application
tests verify this behavior. Primary keys and essential numeric SQL checks are
enforced. Business currency conversion and dimensional marts remain future dbt work.

## File access and deployment plan

Current volume ownership was inspected: UID/GID 10001, with an initially empty
database. The plan adds one one-shot `warehouse_permissions` container, running as
that existing owner rather than root. It sets `/warehouse` to mode 2770 (shared
group plus setgid inheritance), and the existing owned file to mode 0660.

Only the Airflow scheduler/LocalExecutor container receives the warehouse mount
and supplementary group 10001; other Airflow components do not receive the mount.
The writer remains UID 50000/group 0 with that extra group. The warehouse runtime
remains UID/GID 10001. New files inherit the warehouse group. No volume is replaced.

Both loaders and runtime probes take the same `retailflow.duckdb.lock` with mode
0660. This serializes the two DAG writers and cooperating file readers. A health
probe returns `busy` successfully while a loader holds the lock, instead of opening
a competing database connection. Other SQL tools must use this lock protocol or
run only while loading is paused. DuckDB's own file locking remains an additional
boundary; one container or one DuckDB thread alone would not serialize processes.

The reviewed plan adds one permissions initializer and replaces nine images/
containers (warehouse, shared application/API image, and Airflow components/init).
It requests no persistent-volume replacement or deletion. API image replacement
reflects the shared application package/dependency export, not an API schema change.
The shared-state/log/PostgreSQL volumes remain in place.

Before applying: pause both DAGs, wait for running tasks, and verify/back up the
warehouse if it contains data. The current first-load deployment targets the
confirmed empty database. The subsequent [backup deployment](warehouse-backup.md)
adds automatic snapshots and verified recovery. The infrastructure helper itself
still backs up Azurite batches only.
Apply the reviewed saved plan only after approval, then verify initializer exit,
health, and a controlled extraction/load run before restoring schedules.

## Run the working loader locally now

Azurite must be running and `.env` must contain the local key. Install the updated
locked environment (`uv sync --locked`); `pytz` is now explicit because DuckDB's
Python conversion of `TIMESTAMPTZ` values requires it.

```powershell
# Separate validation file; does not touch the managed warehouse:
.\.venv\Scripts\python.exe -m retailflow.warehouse --database artifacts/warehouse-validation.duckdb --source all
# Repeat to demonstrate replay; newly arriving batches can still be loaded:
.\.venv\Scripts\python.exe -m retailflow.warehouse --database artifacts/warehouse-validation.duckdb --source all
```

The CLI prints batches loaded/skipped and rows inserted per source. With active
extraction, another batch can arrive between calls; a newly inserted row then is
new source data, not duplicate replay. Use a fixed manifest set for exact replay
comparisons. `artifacts/` and database/lock files there are ignored by Git.

After the managed image update, inspect counts inside the protected runtime:

```powershell
docker exec retailflow-duckdb python /app/runner.py report
```

That command acquires the common lock before reading table counts.

## Executed evidence and limitations

- The full suite passed **88 tests**, with real Airflow/warehouse validation
  images, PostgreSQL bootstrap, and Azurite enabled.
- Tests exercised canonical row deduplication with full batch lineage, distinct
  dataset identities, decimal FX precision/weekend dates, changed manifests,
  payload corruption, schema changes, conflicting revisions, and full rollback
  when a new record was inserted before a later conflicting record.
- The real images loaded and replayed a batch as Airflow UID 50000 through group
  10001, then read it as warehouse UID 10001 from the same disposable volume.
  Health/writer lock exclusion is tested. No managed data volume was used in tests.
- Real Azurite batches were loaded into a separate validation database. The initial
  snapshot produced 53 unique orders and one canonical FX rate, and immediate
  replay skipped all 11 recorded batches. A later backlog load, with extraction
  still active, reached 277 unique orders, one FX rate, 236 batch ledger entries,
  and 282 order/batch memberships. These are point-in-time counts, not fixed fixtures.
- Terraform validation passed; the live managed warehouse was confirmed empty
  and unchanged while preparing the plan. The subsequent approved deployment verified managed mounting and a no-change
  post-apply Terraform plan.
- Ruff lint/format, Bandit, and the maintained-source secret scan passed. The
  updated local dependency audit reported no known vulnerabilities. Container
  vulnerability scanning remains outstanding. Warehouse backup/restore was added
  and verified in the subsequent linked deployment.

The current implementation scans completed source manifests on each run and
verifies their bytes before ledger lookup. This favors integrity and simple replay
for the small local dataset; large-prefix/large-batch optimization is future work.

This is a working raw layer, not a production deployment. There is no database
RBAC inside DuckDB, encrypted-volume guarantee, retention/deletion workflow,
automatic recovery promotion, correction engine, or completed dbt mart yet. Raw
order data remains synthetic; rates are public reference data with upstream terms.
Schema changes need deliberate migrations; unsupported schema versions fail closed.

References: [DuckDB concurrency](https://duckdb.org/docs/2.0/connect/concurrency),
[DuckDB transactions](https://duckdb.org/docs/stable/sql/statements/transactions).

## Managed deployment verification ? 2026-11-02

The approved saved plan was applied after pausing both DAGs, confirming no active
runs, and confirming that the original warehouse was empty. All 481 backed-up
blobs passed post-apply checksum verification. The permissions initializer exited
0; directory mode is 2770, database/lock mode is 0660, and group is 10001.
The post-apply Terraform plan reported no changes.

Both DAGs were enabled again and triggered with run ID
`warehouse_deployment_20261102`. Both runs succeeded, with all four extract/load
tasks succeeding on attempt 1. At 10:44 Europe/Paris, the managed warehouse held
283 orders, one canonical FX rate, and 243 loaded batches. The latest order was
created at 10:40 Europe/Paris. A direct source replay skipped 237 orders batches
and six FX batches, inserting zero rows. Queries found no raw records missing
first-manifest ledger entries and no order memberships missing canonical records.
Counts are snapshots; enabled schedules continue to add data.

Machine-readable evidence is in the ignored
`artifacts/warehouse-deployment-results.json`. Earlier validation evidence above
remains historical. This deployment demonstrates Terraform-managed file access,
Airflow dependencies, verified raw ingestion, transaction/replay safety, and batch
lineage. Warehouse backup/recovery was subsequently deployed and verified;
dbt transformations remain a future milestone. Existing test coverage verifies rollback and writer exclusion; no
live failure was deliberately injected into the deployed DAGs.
