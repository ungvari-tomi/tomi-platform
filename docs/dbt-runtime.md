# Terraform-managed dbt Core runtime

[dbt freshness and failure-event email](dbt-freshness.md) adds locally prepared
freshness/pipeline actions and current dataset-identity settings. The updated
runtime images await reviewed deployment; ordinary build retains historical-model
behavior and does not implicitly execute source freshness.

[Ten-currency generation and EUR valuation](european-currencies.md) are deployed; the versioned migration and live pipelines are verified.

[Airflow dbt automation](airflow-dbt.md) is deployed; both live three-task verification runs passed.

Browse an independent DuckDB snapshot with [DBeaver using the export helper](database-viewing.md).

Status: deployed locally through Terraform on 2026-11-02. The managed one-shot
check passed against the existing warehouse and persisted its dbt artifacts.
Two [staging models and 29 data tests](staging-models.md) have been built and
verified in the managed warehouse through the local development command.
Both Airflow DAGs run dbt_build after loading. The currency expansion adds
staging.orders_eur and brings the project to three models and 37 tests.

## Responsibilities and isolation

Terraform owns `docker_image.dbt`, one-shot `docker_container.dbt` named
`retailflow-dbt`, and protected volume `retailflow-dbt-artifacts`. dbt is a CLI,
not a SQL server: the container runs its checks and exits. An exited container
with code 0 is the expected state, not an unavailable service. Terraform uses
`must_run=false` and captures the initial check with `attach=true`.

The image uses pinned Python 3.12.16 slim, dbt Core **1.11.16**, dbt-duckdb **1.11.1**,
and the existing DuckDB **2.0.0** and filelock **4.2.5** versions. Its independent
`infra/dbt/uv.lock` resolves dependencies without changing Airflow's constrained
Python environment or the root application's environment. Exported requirements
include exact versions and hashes; Docker installs binary wheels only. Core 1.12
was evaluated but its experimental parser dependency has no binary wheel, so this
runtime deliberately uses the maintained 1.11 patch release. Adapter compatibility
was checked against the [official adapter documentation](https://github.com/duckdb/dbt-duckdb#installation)
and [published Core package](https://pypi.org/project/dbt-core/1.11.16/).

The container runs as UID/GID 10001, with read-only root filesystem, dropped
capabilities, no-new-privileges, no network, no published ports, 512 MiB memory,
one CPU, and temporary HOME on bounded tmpfs. It mounts the existing warehouse
volume and its own artifact volume; there is no backup-volume access or Docker
socket. No cloud account, service credentials, paid service, dbt Cloud, or Git
checkout at runtime is required. Telemetry is disabled, and DuckDB external access
and automatic extension loading/installing are disabled in the local profile.

## Warehouse locking and checks

`infra/dbt/run.py` is the entrypoint. It acquires the same database FileLock with
mode 0660 as loaders, health probes, and backups, and holds it for the complete
CLI subprocess lifetime. Lock acquisition is bounded at 60 seconds; each CLI
command has a 120-second timeout. Missing warehouse files fail instead of letting
the adapter initialize an unintended empty database. CLI failures propagate as
nonzero container exit codes. Invoke dbt through the wrapper; direct CLI calls
would bypass this cooperating lock protocol.

The initial `check` runs `dbt debug --connection`, then `dbt run-operation
runtime_check`. The connection-only debug mode validates the project/profile and
connection without demanding Git in this offline runtime. The macro queries row
counts from declared `raw.orders` and `raw.exchange_rates` sources and fails if
either is missing. These checks establish runtime connectivity and source presence;
they are not completed business data-quality tests. The runtime check creates no staging or mart relations. `parse` validates project parsing; the local development `build` now includes
the prepared staging models and their data tests.

Each invocation writes logs/artifacts to a UUID directory in `/artifacts`, avoiding
stale file reuse. Failed attempts can leave logs there. This volume has no automatic
pruning and remains on the same Docker host. dbt cannot enforce database RBAC in
DuckDB; reviewed project SQL and file access are the trust boundary. The writable
warehouse mount is needed for eventual materializations. Terraform prevention of
volume destruction does not protect against manual Docker deletion or host loss.

## Develop models from VS Code

The development helper uses the already deployed Terraform-built image and mounts
`infra/dbt/project` read-only over its baked-in project. SQL/YAML edits are visible
on the next invocation without rebuilding the image or applying Terraform.

From the repository root:

```powershell
.\scripts\dbt-dev.ps1 parse
.\scripts\dbt-dev.ps1 build
```

If your local execution policy blocks unsigned scripts, run the reviewed helper
in a separate PowerShell process (the policy setting lasts only for that process):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\dbt-dev.ps1 build
```

Create `.sql` models under `infra/dbt/project/models/`, with optional subfolders.
The initial `staging/stg_orders.sql` and `staging/stg_exchange_rates.sql` files now
exist, along with their schema tests and local generic-test macros. See
[staging models](staging-models.md) for their grains, renamed fields, and tests.

`build` creates both staging views in the managed DuckDB and runs their tests.
`parse` validates project structure/Jinja without materializing models; it does
not fully validate SQL semantics against actual data. Saving a file alone does
not execute it. Folder names do not choose SQL schemas: the profile currently
uses `staging`. Materialization `table` produces a table; `ref('stg_orders')` lets
another model depend on the orders view. Use the local mounted project for the
new models; the deployed baked-image source-check container remains unchanged.

The helper defaults to the **shared managed warehouse**, so builds persist their
models and can replace existing model relations. It uses the existing file lock
to coordinate with Airflow/backups; leave source `raw` and operations `ops` tables
to their Python loaders. The helper does not sandbox arbitrary SQL: review your
models/hooks before executing them. To experiment independently, use an already
initialized separate Docker volume with `-WarehouseVolume YOUR_VOLUME`.

The helper checks that the image and volume already exist; it does not provision
infrastructure or create a missing warehouse. It uses a disposable container with
the same offline/non-root/read-only-root controls as the managed runtime. Only
the project directory is bind-mounted. A fresh anonymous artifact volume is copied
to the host before the invocation container/anonymous volume are removed; this
avoids host/container UID conflicts on Linux. No `.env`,
Docker socket, or repository root is mounted. The existing entrypoint holds the
common lock throughout dbt execution and propagates failures. No image rebuild
or runtime-wrapper change is needed. Dependency or baked-in runtime changes still need deliberate review and image
rebuilds through the normal Terraform workflow. Local profile edits are mounted
too; keep the shared warehouse path and lock configuration aligned.

Logs, compiled SQL, manifests and run results remain on the host under
`artifacts/dbt-dev/<invocation-id>/`, ignored by Git. They remain after the
container exits; review `target/run_results.json` and `logs/dbt.log` there.
`-ProjectDirectory` and `-ArtifactDirectory` accept other local paths; all paths
are passed as arguments, including paths containing spaces. `-Image` selects an
existing runtime image. Supported actions are `check`, `parse`, and `build`;
selection flags and other dbt subcommands have not been added to this helper.
The Terraform-owned one-shot container still uses its baked-in project; local
editing does not update that container or create scheduled dbt tasks.

## Runtime commands

Rerun the Terraform-owned check container:

```powershell
docker start -a retailflow-dbt
docker inspect retailflow-dbt --format '{{.State.ExitCode}}'
```

It should exit 0 after reporting both source counts. Logs identify the artifact
directory. To run `parse` in a disposable instance of the Terraform-built image:

```powershell
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 512m --cpus 1 --tmpfs /tmp:rw,noexec,nosuid,size=128m,uid=10001,gid=10001 --mount type=volume,source=retailflow-duckdb-data,target=/warehouse --mount type=volume,source=retailflow-dbt-artifacts,target=/artifacts retailflow-dbt:local parse
```

The image entrypoint still enforces the warehouse lock. Airflow now uses a separate
venv and a baked copy of this same wrapper/project to build the staging views and
run tests after loading. It does not start this standalone container and has no
Docker socket. See the automation guide above for task logs, retries and artifacts.

## Validation and reviewed deployment plan

The real disposable container passed `check` and `parse`, reporting both seeded
raw sources. Removing `raw.exchange_rates` caused a nonzero failure as expected.
The wrapper tests verify writer-lock exclusion, refusal to create a missing
warehouse, CLI failure propagation, and exact DuckDB/filelock version alignment
with the main lockfile. Requirement hashes are checked against the isolated lock.

A second check used a private copy of real landed data: 277 orders, one canonical
FX rate, and 236 batch ledger entries. All seven table inventories were unchanged
after dbt checked both sources. The managed warehouse was not used. Logs and dbt
artifacts were preserved under the ignored path recorded in
`artifacts/dbt-runtime-validation.json`.

The isolated dependency audit found no known vulnerabilities. Ruff lint/format,
Bandit, and Terraform formatting/validation passed. Container vulnerability scanning
has not been executed. The optional dbt image check uses
`RETAILFLOW_TEST_DBT_IMAGE=1` and image `retailflow-dbt:validation`.

The approved plan added three resources (dbt image, one-shot check container, artifact
volume) and replaced seven shared API/Airflow image/container resources because
the application package includes the infrastructure allowlist update. It deletes
or replaces no persistent volume or network; DuckDB runtime and backup volume
remain intact. Before applying, pause existing DAGs, drain active runs, preserve
a verified warehouse backup, then apply and inspect the dbt container exit code,
source counts, and artifacts. Run a post-apply drift check before restoring the
existing extraction/load schedules. Business models and automated dbt tasks are
outside this runtime milestone. The approved deployment was performed on 2026-11-02; see the evidence below.

The full regression suite passed **102 tests**, with Azurite, Airflow, warehouse,
and dbt container integrations enabled. These checks demonstrate isolated dependency
management, adapter connectivity, source declarations, shared-file concurrency,
and Terraform lifecycle preparation. They do not claim deployed dbt transformations.


## Managed deployment evidence ? 2026-11-02

Both extraction DAGs were paused and confirmed to have no active runs. A fresh
warehouse backup was copied to the host and restored to a separate verification
file before applying. That snapshot contained 290 orders, one FX rate, 250 batch
ledger entries, and 295 order/batch memberships across seven tables.

Terraform created the dbt image, artifact volume, and one-shot check container.
The managed check exited 0, connected with Core 1.11.16 / adapter 1.11.1, and
reported 290 raw orders and one raw exchange rate. The paused warehouse's full
table inventory matched the verified pre-deployment snapshot. The container has
only `/warehouse` and `/artifacts` volume mounts. Its generated manifest and logs
were copied to ignored `artifacts/dbt-deployment-artifacts/` as evidence; the
canonical copy remains in the protected artifact volume.

The first apply hit a Docker content-digest error while inspecting the API image
after the dbt check had already succeeded. The API/Airflow services were temporarily
absent. Rebuilding the affected local image tags without provenance attestations
restored Docker image inspection. A new reviewed plan recreated only the remaining
approved API/Airflow resources and replaced the initialization container; no data
volume was deleted or replaced. This was a local image-metadata repair, not a
warehouse recovery or infrastructure redesign.

Machine-readable evidence is in ignored `artifacts/dbt-runtime-deployment.json`;
the independent pre-deployment snapshot is in `artifacts/pre-dbt-deployment/`.
The 102-test suite passed before deployment, and no application code changed
during deployment. No staging models, business tests, or automated dbt tasks have
yet been added. Recommended next milestone: staging models and data-quality tests,
followed by reviewed Airflow integration using an isolated dbt environment.

After completing the recovery apply, Terraform reported no changes. All 513
backed-up blobs passed checksum verification. The managed dbt container was
confirmed exited 0, UID/GID 10001, network mode none, and read-only root filesystem.
Existing extraction schedules were restored after service readiness checks.

Both `dbt_deployment_20261102` verification runs succeeded. The orders run
completed both extract and load tasks successfully, and the exchange-rate run
also succeeded. This verifies that the restored ingestion stack still works.


## Development command verification

Two opt-in integration tests passed for `scripts/dbt-dev.ps1`. Using disposable
storage and project/artifact paths containing spaces, two builds from the same
image picked up a local SQL edit and changed `staging.dev_probe` from revision 1
to revision 2. The YAML not-null test passed, logs/run results persisted locally,
and no target/log files were written into the mounted project. A nonexistent
warehouse volume caused a nonzero failure without creating that volume. The test
models existed only in temporary test storage; no business models were added to
the managed project or warehouse. Ruff lint/format and whitespace checks passed.
No infrastructure or dependency changes were needed for this development helper.
