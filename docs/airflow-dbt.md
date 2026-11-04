# Airflow dbt builds and data tests

The opt-in [customer pipeline](customer-pipeline.md) adds a separate paused DAG
and customer-only dbt selection; deployment is pending review.

[dbt freshness and failure-event email](dbt-freshness.md) was deployed on 2026-11-04.
It supersedes the single-build launcher below with a locked
source-freshness -> freshness-tests -> build pipeline and a 450-second parent
timeout. The new current source passes only dataset identity settings in addition
to the existing credential-free child environment, and retains each phase's artifacts.
The previously deployed behavior and verification history follow.

[Ten-currency generation and EUR valuation](european-currencies.md) are deployed; the versioned migration and live pipelines are verified.

Status: deployed locally through Terraform on 2026-11-02. Both live DAGs execute
extract -> load -> dbt_build and remain enabled. Controlled runs succeeded through
all six tasks. The subsequent currency expansion adds a third model and increases
each current build to 37 data tests; see the migration guide above.

## Task ordering and execution

Both deployed DAGs contain `extract -> load -> dbt_build`. The third task
uses the default all-success trigger, so an extraction/load failure prevents its
execution. Both source DAGs build the complete three-model project and run
all 37 data tests, including when a rate request reuses an existing publication.
This checks current warehouse contents across both sources rather than only the
last batch. Existing ten-minute orders / daily 17:00 UTC rates schedules,
max_active_runs=1, catchup=False, and retry policies remain unchanged.

`retailflow/transformation.py` launches `/opt/dbt/venv/bin/python -I
/opt/dbt/run.py build` from an ordinary TaskFlow task. It imports no dbt packages
in Airflow's interpreter. The Airflow image includes a separate immutable virtual
environment installed from the same hash-checked `infra/dbt/requirements.txt` used
by the standalone runtime, plus the shared wrapper and project. Dependencies are
installed only at image build time. There is no Docker socket mount, new executor,
remote dbt service, or runtime package installation. This follows Airflow's
[dependency-isolation guidance](https://airflow.apache.org/docs/apache-airflow/3.3.2/best-practices.html#handling-conflicting-complex-python-dependencies).

The existing wrapper locates dbt inside that venv and holds the common 0660
warehouse FileLock for the full CLI lifetime. The LocalExecutor scheduler remains
UID 50000/group 0 with supplementary group 10001; no new volume permissions or
warehouse mounts are needed. Extraction/load writers, manual builds, backups and
probes keep the same lock protocol. Simultaneous source DAG builds serialize.
The wrapper waits up to 60 seconds for the lock and limits the dbt CLI to 120
seconds; its task launcher has a 150-second subprocess timeout, inside the
existing task execution timeout. Large projects may need reviewed timeout changes.

The child process receives a minimal environment: its venv PATH, private HOME,
locale, Python isolation settings, warehouse path and artifact path. Airflow DB,
API, Azurite credentials and PYTHONPATH are not forwarded. User-site packages are
disabled; the dbt interpreter does not import Airflow. The inherited operating
system identity, container filesystem and network are still shared with the
scheduler. This isolates dependencies and reduces credential exposure; it is not
a sandbox for untrusted SQL/Python macros. DAG/project authors remain trusted.

## Failure handling and artifacts

A nonzero dbt exit or launcher timeout raises an exception to Airflow. Artifacts
that are missing, empty, failed, errored, or skipped also fail the task even if a
CLI unexpectedly returns zero. Quality failures are never converted into success.
The task inherits two retries and the source DAG's retry delay, preserving each
attempt separately. Retrying a data-quality failure can fail again until data or
models are corrected. Created views can remain after test failure; dbt build is
not an atomic publication or warehouse access-control boundary.

A hashed DAG/run/attempt identity plus UUID produces a unique directory below
`/opt/airflow/logs/dbt/`, on the existing protected Airflow logs volume. It contains
`task.json`, a private HOME, and the shared wrapper's UUID artifact directory with
`target/run_results.json`, manifest, compiled SQL, and `logs/dbt.log`. Both successful
and failed attempts remain available. dbt CLI output is inherited by the Airflow
task log, and the launcher logs the artifact directory before execution. Only a
small artifact-path/model-count/test-count summary enters XCom. No rows or full
manifest are pushed through XCom. Log retention/pruning and external alerts are
not implemented; disk usage grows and follows the existing private operational
log classification.

Airflow uses the project baked into its image. Local SQL edits through
`dbt-dev.ps1` do not update scheduled code. Terraform's Airflow image triggers now
hash the wrapper, requirements and SQL/YAML project files; reviewed image updates
are needed to schedule later model changes. The standalone dbt image is also
updated by this plan to include the already verified staging project. Existing
raw schemas, conversion logic, source APIs, backups and snapshot commands are
otherwise unchanged.

## Validation and deployment plan

Unit tests verify minimal child environment, path-safe task identity, propagated
CLI failure/timeouts, preserved retry metadata, and rejection of failed/skipped
run artifacts. The real Airflow SDK constructs both DAGs with three tasks and the
extract/load/build edges, all-success dependency, and prior manual FX-date regression.

The real container test runs the actual DAG `dbt_build` Python callable as UID
50000/group 0 plus group 10001, using disposable warehouse/log volumes. It confirms
that the copied venv can import dbt but not Airflow, builds both views and passes
all 29 tests, then corrupts only fixture data. The next task call raises dbt's
nonzero failure, and run results/metadata from both attempts remain. These are
actual callback/CLI checks, not a live scheduler-driven DAG failure injection.
Managed data volumes are not used in these tests.

The approved plan applied **nine image/container replacements**, no creates,
updates or standalone deletions: Airflow image/init/three services, shared API
image/container, standalone dbt image/check container. API replacement reflects
shared application package/context changes. All persistent data, backup, dbt
artifact, PostgreSQL and Airflow volumes and the Docker network are preserved.
DuckDB runtime is not replaced. Dependencies are unchanged from the audited dbt
lock; no new dependency audit or full image vulnerability scan is claimed.

Before applying, pause both existing DAGs and drain active runs, preserve a
verified independent warehouse snapshot, and apply the reviewed plan only with
approval. Verify the standalone check exit, API readiness, three-task DAG discovery,
controlled source runs through dbt_build, persisted run results, source checksums,
and a post-apply no-drift plan before restoring schedules. The approved apply and controlled scheduler-executed runs were subsequently
verified as described below. A separate clock-triggered dbt run was not awaited
as part of deployment; both schedules remain enabled with the updated tasks.

Ruff lint/format, Bandit, Terraform formatting/validation, and whitespace checks
passed. A maintained-source secret scan of the new launcher, DAG, Dockerfile and
Terraform orchestration file found no findings. The hash-pinned dbt lock is unchanged
from its earlier successful dependency audit. The full regression suite passed **114 tests in 205.71 seconds**, with Azurite,
Airflow, DuckDB and dbt image opt-ins enabled. The final Airflow image test exercised
the updated private HOME and minimal environment along with the full quality-failure
path. No code changed after this full-suite run.


## Managed deployment verification ? 2026-11-02

Both source DAGs were paused and no active/queued runs remained. A locked backup
was copied to the host and restored with its original catalog filename, and both
staging views remained queryable: 331 orders and two canonical exchange rates.
The independent snapshot is recorded in
`artifacts/airflow-dbt-predeployment-backup.json`.

The saved plan was applied with `-parallelism=1` through the existing secret-safe
helper to serialize image operations following the earlier Docker image error.
No permanent helper/code changes were made during deployment. The apply succeeded,
all 597 backed-up blobs verified unchanged, the updated standalone dbt check
exited 0, and the subsequent Terraform plan reported no changes.

After API readiness and three-task discovery were verified for both DAGs,
controlled runs `airflow_dbt_deployment_20261102` were queued and the DAGs enabled
so the scheduler could execute them. Both runs succeeded; extract, load, and
dbt_build all succeeded on attempt 1 in each DAG. Each persisted dbt run-results
file contained two successful view models and 29 passing tests (31 successful
entries, with no failures/skips). Task metadata and dbt artifacts remained on the
protected logs volume and were also copied to ignored
`artifacts/airflow-dbt-run-artifacts/` for deployment evidence.

Machine-readable task states and artifact paths are retained in ignored
`artifacts/airflow-dbt-deployment.json`. Both schedules were left enabled after
verification. No live bad data was injected; failure behavior was tested only
with disposable fixtures before deployment. The 114-test suite passed before
apply and was not redundantly repeated without code changes. Existing limitations
on alerting, log retention and untrusted project code still apply. Next recommended
milestone: validate historical exchange-rate coverage, then build a date-aware
EUR-valued order model with explicit missing-rate handling.
