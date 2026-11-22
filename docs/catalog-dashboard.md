# Recurring catalog reporting and Streamlit views

Deployed locally on 2026-11-22, with recurring catalog ingestion **active** every
six hours UTC. Open <http://127.0.0.1:8501> and select **Catalog and inventory**.
The AI business analyst is
a later, separately instructed milestone; no AI API, key, dependency or service
is introduced here.

## Data and refresh behavior

The existing sales overview and its immutable snapshot format remain intact.
The catalog workflow additionally publishes a separate snapshot under
`/reports/catalog/<version>/retailflow.duckdb`, with its own `current.json` pointer.
The dashboard's **View** selector opens **Catalog and inventory**, containing:

- **Category sales:** one sales dataset at a time, category/status/country/date
  filters, exact covered EUR revenue, valuation coverage and category counts.
- **Inventory movements:** one seed/interval profile at a time, category/status/
  warehouse filters, inbound/outbound units, movement counts and net units moved.
- **Product catalog:** 20 bounded synthetic products, with SKU, name, category,
  status, revision and source/observation timestamps; retired products remain.

Net inventory movement has no supplied opening balance and must not be presented
as stock on hand. Sales use the existing exact per-order EUR rounding and retain
uncovered orders in counts. Groups without usable FX show unavailable revenue.
Missing product matches stay visible as **Unmatched**. Product metadata is the
latest observed catalog, not historical attributes valid at order time.

The original sales overview continues its existing orders/FX publication cadence.
Catalog/category/inventory snapshots publish with the six-hour catalog workflow,
not continuously from Kafka. The page shows publication, catalog-check and latest
inventory times; **Refresh catalog snapshot** loads the latest verified version.
It does not add freshness polling or interpret browser time as source health.
MongoDB metadata may remain unchanged between snapshots; repeated reads confirm
current source contents without inventing product revisions.

## Build, checks and publication

When catalog and dashboard features are enabled, the catalog dbt task uses
`catalog-reporting-pipeline`. Plain `catalog-pipeline`, ordinary builds and the
original `reporting-pipeline` keep their existing behavior.

The new action holds the shared warehouse FileLock (0660) throughout:

1. Four source freshness checks: current-profile orders, FX ingestion, catalog
   ingestion and current-profile inventory movements.
2. Four tagged checks: order/FX clocks and pair coverage, per-product catalog
   freshness and empty/stale/future inventory observations.
3. The catalog models plus `daily_sales` and its tested parents.
4. Catalog snapshot publication, retaining separate source/test/build artifacts.

Freshness stays in dbt: orders/inventory use 30 minutes, FX ingestion 30 hours
with the existing seven-day publication allowance, and catalog snapshots 24 hours.
Any nonzero result stops publication and fails the DAG through its existing
failure-only callback. No recovery/success/reminder email or new provider is added.

The snapshot contains exactly three allowlisted tables:

| Table | Permitted fields and grain |
| --- | --- |
| `catalog_sales` | Existing daily-sales dataset/date/country/product/currency aggregate, exact cents/coverage/counts, plus synthetic product name/category/status |
| `inventory_movements` | Seed/interval/product/warehouse aggregate, synthetic name/category/status, event count, net/inbound/outbound units and latest event time |
| `products` | Product ID, SKU, synthetic name/category/status, revision, update time and first observation of that revision |

No raw orders, customer records/tokens, MongoDB documents, embedded attributes,
variants, memberships, source credentials or warehouse file are copied. The
repository owner owns this synthetic reporting dataset; approved use is local
retail analytics and engineering demonstrations. This is not anonymization or
a legal-compliance claim. A synthetic marker cannot detect personal text entered
by a privileged operator; real-person imports require separate review.

The publisher creates a fresh file under an unpublished directory, enforces table
names/types/keys/row contracts, validates closed bytes, and publishes its checksum
manifest before atomically replacing the catalog pointer. Files are 0640; the
protected reporting directories retain group 10001/setgid 2770. Failed or pending
attempts and old versions are retained for explicit owner review, never deleted
automatically. Original sales pointers/versions are not overwritten by catalog
publication. Independent snapshots and source backups remain separate from the
live warehouse volume; restore still targets a new file.

The read-only dashboard validates format `catalog-v1`, UUID paths, bounded size,
SHA-256, exactly three base tables, column names/types, row counts, enum/key rules,
coverage, movement arithmetic and product-label consistency. It caches immutable
versions, receives no warehouse mount or source credentials and keeps loopback-only
port 8501. Errors are sanitized before display. Dynamic SQL identifiers in the
publisher/reader come only from fixed in-code allowlists; values are parameterized.
The reviewed Bandit suppressions cover those fixed identifiers, with a regression
test rejecting caller-supplied SQL as a table name.

## Operation and verification

The existing environment flags remain `RETAILFLOW_CATALOG_ENABLED` and
`RETAILFLOW_DASHBOARD_ENABLED`; defaults remain false for new installations. New
installations start their DAG paused for verification. Recurring activation of
this local installation is an explicit owner action recorded in deployment
evidence, independent from Terraform's runtime-enablement flags. Docker and
Airflow must be running for the local schedule to execute.

```powershell
uv run --locked python -m pytest tests/test_catalog_reporting.py tests/test_reporting.py tests/test_transformation.py -q -p no:cacheprovider
docker build -f infra/dbt/Dockerfile -t retailflow-dbt:validation infra/dbt
docker build -f infra/dashboard/Dockerfile -t retailflow-dashboard:validation .
$env:RETAILFLOW_TEST_DASHBOARD_IMAGE = '1'
uv run --locked python -m pytest tests/test_catalog_dashboard_container.py tests/test_dashboard_container.py -q -p no:cacheprovider
Remove-Item Env:RETAILFLOW_TEST_DASHBOARD_IMAGE
```

The container tests use anonymous disposable warehouse/reporting volumes and
the actual non-root identities. They verify both Streamlit pages with AppTest,
filters/profile isolation, missing FX, read-only mounts, no warehouse/secrets in
the UI, full dbt publication and preservation of pointers after freshness failure.
Unit tests include cents beyond JavaScript's safe integer range, malformed
manifests, extra tables/columns, corrupted bytes, immutable prior versions,
shared-lock enforcement and the sales snapshot's compatibility.

No new subscription, cloud resource, runtime dependency or orchestrator is needed.
Terraform continues to own the images, runtime and protected reporting volume;
Dockerfile-specific build allowlists and hashes track the new reader/page modules.
Existing API/Kafka resources are outside this deployment's replacement scope.

## Deployment evidence

The foundation commit `2f9b8a7` passed hosted CI before activation. New extension
changes were validated locally: the focused reporting/wrapper/style/hash suite
passed 45 tests (one opt-in skipped), the selector/launcher suite passed 40, and
both real dbt/Streamlit container tests passed. Ruff, SQLFluff, reviewed-allowlist
Bandit checks, workflow syntax, Terraform validation and source safety passed.
These local suites overlap; their counts are not a unique combined total.

The reviewed saved plan replaced ten Airflow/dbt/dashboard image/runtime resources,
without replacing API, Kafka, PostgreSQL or protected volumes. Prior DAG states and
workers were recorded, writers drained/stopped, and a locked warehouse snapshot
plus 5,624 verified landed blobs were preserved. Those original blob checksums
were reverified after deployment. All original workflows/workers were restored;
catalog pause state was intentionally changed to active. Unrelated container IDs
and the existing sales snapshot's checksum were preserved.

An immediate **manual** verification run completed extraction, loading and the
enhanced dbt action: eight models, 67 passing build tests, four source freshness
checks and four tagged freshness tests. It published a verified three-table
snapshot with 2,289 sales aggregates, 59 inventory aggregates and 20 products.
The running dashboard's AppTest verification confirmed actual displayed values,
all three tabs, its non-root read-only identity and continued original-sales
compatibility. These counts describe the captured snapshot, not fixed limits.

The DAG remains unpaused. Its next cron run was recorded as 2026-11-22 12:00 UTC
(13:00 Europe/Paris); that future timed execution was not observed during this
deployment. Platform health was `ok` and the post-deployment Terraform plan showed
no changes. Private plans, backups, the prior-commit CI evidence and verification
reports are retained under ignored `artifacts/catalog-dashboard-activation/`.
No commit, push or hosted CI run for the new extension is claimed here.
