# Browse DuckDB with DBeaver

Use a verified local snapshot to browse tables and run exploratory SQL without
holding a connection open against Airflow's warehouse. This helper is implemented
and tested; it does not change Terraform resources or install DBeaver.

## Export a current snapshot

From the project root, with Docker Desktop and the managed DuckDB runtime running:

```powershell
.\scripts\export-warehouse.ps1
```

If PowerShell blocks unsigned scripts, invoke the reviewed helper in a separate
process with a process-only execution policy:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\export-warehouse.ps1
```

The equivalent Python command is:

```powershell
.\.venv\Scripts\python.exe -m scripts.export_warehouse
```

The command prints the full verified database path, capture timestamp, DuckDB
version, and table counts. Files are stored under ignored
`artifacts/warehouse-snapshots/<unique-export-id>/`. Open **retailflow.duckdb** there.
The original filename is preserved because dbt views can contain qualified
`retailflow.raw.*` references. Do not rename the exported file.

To choose another local destination:

```powershell
.\scripts\export-warehouse.ps1 -OutputDirectory 'C:\my-data\DuckDB snapshots'
```

Each run creates a new snapshot path and preserves all previous snapshots. To
refresh DBeaver's data, rerun the export and open a connection to the newly printed
file. An existing GUI connection stays on its original point-in-time snapshot;
it does not automatically show subsequent Airflow or dbt changes.

## Open it in DBeaver

1. Install DBeaver Community if needed.
2. Choose **Database > New Database Connection > DuckDB**.
3. Select the exported `retailflow.duckdb` file as the database path and let
   DBeaver download its JDBC driver if prompted.
4. Test the connection, finish, and expand the connection's schemas/tables.
5. Open an SQL editor for that connection and run:

```sql
select *
from raw.orders
order by created_at desc
limit 100;
```

Other starting points are `raw.exchange_rates` and `ops.loaded_batches`. Any dbt
models you have built are included too; the current profile puts them in `staging`.
Use a DuckDB JDBC driver compatible with the exported database version printed
by the command (currently DuckDB 2.0.0). See the official
[DuckDB DBeaver guide](https://duckdb.org/docs/stable/guides/sql_editors/dbeaver)
for connection and driver configuration. Do not configure a connection to the
live Docker volume. Queries or edits in the snapshot cannot update the managed
warehouse; exploratory changes affect only that exported copy.

## Integrity and operational boundaries

The host helper invokes the deployed `/app/runner.py backup` command. That takes
the shared FileLock, checkpoints and closes DuckDB, then publishes a completed
snapshot. The helper copies the immutable completed bundle from `/backups` and
uses the existing restore verifier to check file SHA-256, length, SQL readability,
and base-table row counts before publishing `snapshot.json` and printing success.
Views are carried in the database; the original catalog filename is retained.
A checksum failure leaves no verified snapshot or success marker. Failed transfers
can leave an incomplete export folder; do not open it as a verified export.

The writer lock is held only during the server-side snapshot, not while you browse
its independent host copy. Airflow/dbt/backups continue using the live warehouse.
Docker access and the locked Python environment are required. The command does not
pause DAGs, install dependencies, rebuild images, or change infrastructure.

Exports contain the entire warehouse and provenance. Treat them with the same
classification as the source; keep them local and out of Git. A normal export
retains a server-side backup plus a host backup bundle and verified browsing copy.
There is no automatic cleanup or retention expiry. They are not off-host disaster
recovery unless you separately copy them to another host. A GUI may alter its copy,
so the export verification describes the state at export time, not later edits.

## Executed checks

Two automated tests passed. A damaged transfer was rejected without a published
snapshot; repeated real Docker exports produced different paths while the first
file remained open. The first copy retained one row, and the refreshed copy had
two rows after the source changed. A view referencing `retailflow.raw.orders`
remained queryable in both exports. Tests used disposable volumes and an output
path containing spaces. The PowerShell helper also exported the real managed
warehouse successfully. Ruff, Bandit, and whitespace check results are recorded
with the milestone. DBeaver itself was not installed or tested in this session.
