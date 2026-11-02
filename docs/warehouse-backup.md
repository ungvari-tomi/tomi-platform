# DuckDB backup and recovery

Browse an independent DuckDB snapshot with [DBeaver using the export helper](database-viewing.md).

Status: deployed locally through Terraform on 2026-11-02. The automatic startup
backup and separate-file restore drill passed; the populated warehouse was preserved.

## Design and ownership

Terraform creates protected volume `retailflow-duckdb-backups` and mounts it only
at `/backups` in the warehouse runtime, owned by UID/GID 10001. Airflow receives
no backup mount. The existing warehouse/data volume is preserved. The updated
runtime takes one backup on startup, then every 86400 seconds after a successful
backup. Failures log `warehouse_backup_failed` with the exception type and retry
in 60 seconds. Health probes still measure database availability, not backup age;
there is no external backup-failure alerting yet.

Each backup acquires the same database FileLock (0660) as loaders and probes,
opens DuckDB, executes CHECKPOINT, and closes the connection before copying.
The lock remains held until the copied file has passed a read-only SQL inventory.
CHECKPOINT consolidates the WAL into the database file; see the official
[DuckDB checkpoint documentation](https://duckdb.org/docs/2.0/sql/statements/checkpoint).
Uncoordinated external writers are unsupported. For the small current database,
copying and inventory under the lock is acceptable; large databases will delay
loads. The lock wait is bounded at 60 seconds.

A snapshot is first written to a unique `.pending-*` directory. Data and manifest
are flushed/fsynced, and the directory is renamed to a completed bundle only after
success. The manifest records format version, UTC capture time, file SHA-256,
byte length, DuckDB version, and per-table row counts. Recovery copies to a new
file, verifies its bytes, opens it read-only, and compares the table inventory.
It refuses an existing target or target WAL and removes its own failed partial
copy. This is local integrity checking, not a signed or tamper-proof archive.
A process crash may leave an incomplete `.pending-*` directory; it is not a
completed backup and must not be selected for recovery.

Snapshots contain the entire warehouse, including provenance. They stay local and
inherit the source data classification. There is no automatic deletion or retention
expiry: disk usage grows by approximately one database file per snapshot, including
startup snapshots. Review disk capacity and copy important bundles off the Docker
host. Both local volumes are vulnerable to Docker/host loss. Encryption, off-host
replication, retention automation, and recovery time guarantees are not implemented.
The intended backup interval is 24 hours; there is no guaranteed recovery-point
objective if the runtime is stopped or backups are failing. Source blobs also
remain available for replay, independently of these snapshots.

## Commands

Create an additional backup:

```powershell
docker exec retailflow-duckdb python /app/runner.py backup
```

The JSON response and `warehouse_backup_completed` runtime log contain the exact
completed bundle path. Use that path in a drill; do not select `.pending-*` folders:

```powershell
# Replace the bundle path with the completed path from the backup output.
docker exec retailflow-duckdb python /app/runner.py restore --bundle /backups/COMPLETED_BUNDLE --target /warehouse/recovery-drill.duckdb
docker exec -e RETAILFLOW_DUCKDB_PATH=/warehouse/recovery-drill.duckdb retailflow-duckdb python /app/runner.py report
```

Choose a new target name for every drill. The CLI never replaces the live database.
A verified restored file is a candidate for recovery, not an automatic cutover.
For an actual incident, pause both DAGs, wait for active tasks, preserve the damaged
file for diagnosis, verify the recovered file, and review the intended promotion
and infrastructure configuration before replacing/repointing the managed warehouse.
That potentially destructive promotion needs explicit approval. Do not discard or
reset extraction checkpoints: the restored batch ledger makes retained landing
batches safe to replay. Missing snapshots or manifests should stop recovery rather
than trigger an empty database replacement.

## Executed evidence

The real-data drill used the separate validation warehouse, never the live managed
file. Recovery verified all seven raw/operations tables, including 277 orders, one
FX rate, 236 loaded batches, and 282 order/batch memberships. Machine-readable
evidence is in ignored `artifacts/warehouse-recovery-validation.json`.

Tests cover restored decimal values and ledger contents after source deletion,
checksum corruption, inventory mismatch, missing source, writer-lock exclusion,
refusal to overwrite a target or WAL, and cleanup of rejected copies. The opt-in
Docker test starts the actual automatic-backup runtime as UID 10001 with a read-only
root filesystem and separate anonymous data/backup volumes, then verifies recovery
from another container after that runtime exits. Managed data volumes are not used.

The full suite passed **97 tests**, including the real Azurite/Airflow/warehouse
image integrations and the automatic-backup container test. Ruff lint/format,
Bandit, Terraform formatting/validation, and `git diff --check` passed. Dependencies
were unchanged; no new dependency audit or container vulnerability scan is claimed.

The applied Terraform plan created only `docker_volume.duckdb_backups` and replaced
ten images/containers: DuckDB image/runtime/permissions initializer, shared API
image/container, Airflow image/init and three components. The shared application
package includes the infrastructure helper allowlist change, so its image hashes
also change. No persistent volume or network is deleted or replaced. The approved plan was applied after pausing both DAGs and taking a verified
independent host snapshot of the populated live warehouse.
Future updates should also pause both DAGs and preserve a consistent snapshot
before replacement. A live database promotion still requires separate approval.


## Managed deployment evidence ? 2026-11-02

The pre-deployment snapshot was copied to the host and restored independently,
verifying 286 orders, one FX rate, and 246 batch ledger entries before replacement.
The new runtime automatically published its first completed backup at 11:18:51
Europe/Paris. The snapshot was 5,779,456 bytes and passed SHA-256 validation.
Recovery into a separate `/warehouse/backup-drill-*.duckdb` file matched all seven
tables and their row counts, including 291 order/batch memberships. The live file
was not replaced. The independent host snapshot and drill file were retained.

Post-apply verification confirmed 504 backed-up blobs unchanged, a healthy non-root
warehouse runtime with read-only root filesystem, permissions initializer exit 0,
no Airflow backup mount, and no Terraform drift. The configured next backup is
86400 seconds after the successful startup backup while the runtime stays running;
restarting the runtime takes another snapshot. Waiting a full 24 hours was not
part of this deployment check. The 97-test suite passed before deployment; no
application code changed during deployment.

Machine-readable deployment evidence is saved locally in ignored
`artifacts/warehouse-backup-deployment.json`; the independent host snapshot is in
`artifacts/pre-backup-deployment/`. Daily backups remain on the same Docker host:
off-host protection, pruning, and backup-age alerting remain future work. Recommended
next milestone: dbt staging models and quality tests over the raw warehouse.

Both schedules were restored after these checks. The manually triggered
`backup_deployment_20261102` runs succeeded for both orders and exchange rates,
confirming that extraction and downstream loading still work after replacement.
