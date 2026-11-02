# Terraform-owned DuckDB runtime

[Daily warehouse backups and recovery](warehouse-backup.md) are deployed, with verified startup backup and recovery.

The [raw loader and shared-volume extension](warehouse-loading.md) were deployed
on 2026-11-02. They add serialized Airflow access, group permissions, raw tables,
and a lock-aware health probe.

Status: deployed and healthy. The managed image, container, and protected volume
were applied as part of the 15-resource [Airflow deployment](orchestration.md).
The original deployment initialized an empty database; the subsequent loader
deployment adds raw records and batch lineage.

## Ownership and operation

Terraform owns the local image build, runtime container, persistent volume, network
attachment, environment configuration, health check, CPU/memory limits, and runtime
security settings. `infra/local/duckdb.tf` defines these resources; Terraform loads
it alongside `main.tf`. There is no cloud account or additional hosted service.

DuckDB is an embedded library, not a standalone SQL server. The image runs a small
Python bootstrap that opens `/warehouse/retailflow.duckdb`, creating an empty file
if needed, executes `SELECT 1`, closes the connection, and waits for termination.
This makes initial file creation part of the container startup controlled by
Terraform. It does not create business tables, ingest data, or implement scheduling.
Future schemas, migrations, loads, and SQL transformations belong to application
code with transaction/replay tests, not Terraform provisioners or shell SQL hacks.

After reviewing the saved plan and explicitly approving its application:

```powershell
.\.venv\Scripts\python.exe -m retailflow.infrastructure apply
.\.venv\Scripts\python.exe -m retailflow.infrastructure plan
docker exec retailflow-duckdb python /app/runner.py check
```

The approved apply built the managed image, created `retailflow-duckdb-data`, and
started `retailflow-duckdb`. `/warehouse/retailflow.duckdb` exists and passes its
SQL probe. The post-apply plan has no changes. A separately built validation
image remains a test artifact.

For temporary shutdown after deployment:

```powershell
docker stop retailflow-duckdb
docker start retailflow-duckdb
```

The volume survives container restart/replacement and has `prevent_destroy=true`.
Never remove it to stop the service. Keep the volume resource in configuration:
removing its configuration also removes Terraform's lifecycle guard. The runtime
now takes daily checkpointed backups to a separate protected volume; see
[backup and recovery](warehouse-backup.md). The infrastructure helper's blob
backup still protects Azurite only. Preserve a verified warehouse snapshot before
replacing a runtime with loaded data.

## Reproducibility and boundaries

- Python 3.12.16 slim Bookworm is pinned by manifest digest in the Dockerfile.
  DuckDB 2.0.0 matches the root `uv.lock`. Installation requires wheel hashes;
  Linux CPython 3.12 x86_64/aarch64 hashes are recorded in `requirements.txt`.
  Update those pins together when updating dependencies. Only x86_64 was tested.
- The Docker build context is only `infra/duckdb`, with an allowlist `.dockerignore`.
  `.env`, Terraform state/plans, application data, and host virtual environments
  cannot enter the build context. File hashes trigger image rebuilds when inputs change.
- The runtime uses UID/GID 10001, a read-only root filesystem, no Linux capabilities,
  and `no-new-privileges`. Only `/warehouse` and a 64 MiB tmpfs `/tmp` are writable.
  The volume receives its initial directory ownership from the image.
- Terraform limits the container to 1 CPU and 512 MiB RAM. DuckDB is configured
  for one worker thread and a 256 MB SQL memory budget. The SQL budget is not a
  total-process memory guarantee; the Docker limit is the outer boundary.
- No ports are published. The container joins the existing local Docker network
  for future Azurite access, but no loader, Blob SDK, source credentials, or network
  retrieval is included in this minimal image. Host-only BlobStore currently uses
  loopback by default and now supports one explicitly approved internal container
  endpoint for Airflow. A warehouse loader still needs to be implemented.
- A read-only SQL probe runs every 30 seconds. The bootstrap does not keep the
  database connection open. Future write jobs must be serialized: one container
  and one SQL thread do not themselves enforce a single writer process. DuckDB
  file locking applies. The separate health probe can encounter a lock while a
  writer holds the file; coordinate probes and writers when adding ingestion.
- `restart=no` matches the current local runtime policy; `must_run=true` expresses
  desired running state during Terraform operations. No automatic crash recovery
  service or orchestrator is claimed.

Reference: [DuckDB concurrency](https://duckdb.org/docs/2.0/connect/concurrency).
The local setup is free of mandatory subscriptions, trials, or cloud provisioning;
image/package downloads and your own local compute/storage are still required.
No public registry push, cloud deployment, container vulnerability scan, database
authorization system, encryption-at-rest control, or business data quality claim
is part of this step.

## Executed verification

On 2026-10-31:

- Terraform format/validation passed and live planning reported three additions,
  no imports/updates/replacements/deletions, and no Azurite changes.
- The digest-pinned image built successfully, including hash-verified wheel installation.
- All 64 tests passed with live Azurite and disposable DuckDB image tests enabled.
  The container test verified UID/GID, read-only root, capability drop, CPU/memory
  limits, initial write permissions, and reopening a database from another container
  through the same test volume. Its temporary containers/anonymous volume were removed.
  A strengthened rerun also passed: the process had UID 10001 and an attempted
  write under `/var/tmp` failed with the kernel's read-only-filesystem error.
- Unit tests verified an empty database has no business tables, the health probe
  cannot silently create a missing database, dependency pins match the project
  lockfile, and the plan guard refuses destruction of the warehouse volume.
- Ruff and Bandit passed. The two existing narrow subprocess suppressions remain
  documented in the Terraform runbook. No new Python dependencies were added.

To repeat image verification without applying Terraform:

```powershell
docker build --tag retailflow-duckdb:validation infra/duckdb
$env:RETAILFLOW_TEST_AZURITE = '1'
$env:RETAILFLOW_TEST_DUCKDB_IMAGE = '1'
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
Remove-Item Env:RETAILFLOW_TEST_AZURITE
Remove-Item Env:RETAILFLOW_TEST_DUCKDB_IMAGE
```

Without the DuckDB image flag, the disposable-container test is skipped. No
disposable test uses the protected `retailflow-duckdb-data` volume. Managed file
initialization and SQL health were verified after apply. Persistence of the populated file across managed-container replacement, automatic
backup, and separate-file recovery were verified on 2026-11-02; see the linked
backup deployment evidence.
