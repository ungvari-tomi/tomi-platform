# Initial dbt staging models

[Ten-currency generation and EUR valuation](european-currencies.md) are deployed; the versioned migration and live pipelines are verified.

[Airflow dbt automation](airflow-dbt.md) is deployed; both live three-task verification runs passed.

Status: built in the managed warehouse with user approval on 2026-11-02, after
validation in disposable storage. Both views and all 29 data tests passed.
The mounted local project now contains these models; the Terraform-owned check
container still uses its previously baked project. Airflow dbt scheduling, image
updates, currency conversion, and marts are separate future work.

## Models and grain

| Model | Grain | Key |
|---|---|---|
| `staging.stg_orders` | One synthetic order within a generator dataset | dataset_id, order_id |
| `staging.stg_exchange_rates` | One published provider/currency-pair rate on a date | rate_provider, base_currency_code, quote_currency_code, rate_date |

Both are views with explicit field lists and casts. They preserve every source row
and do not silently filter invalid data or deduplicate conflicting keys. Python
continues to own raw loads, schema constraints, and batch provenance. Required
source fields map as follows:

| Source | Staging |
|---|---|
| orders.created_at | ordered_at (TIMESTAMPTZ) |
| orders.country / currency | country_code / currency_code |
| orders.first_manifest | source_manifest_path |
| exchange_rates.provider | rate_provider |
| exchange_rates.base / quote | base_currency_code / quote_currency_code |
| exchange_rates.rate | quote_units_per_base_unit (DECIMAL(24,12)) |
| exchange_rates.first_manifest | source_manifest_path |

Order identifiers are only unique within a dataset; an ID repeated in a different
dataset is valid. Monetary values remain integer minor units in their original
currency. No floating-point conversion or cross-currency sum is introduced.
EUR/HUF rates express HUF per one EUR; rate_date is the publication date, not the
request date. Weekend request history remains in raw operations provenance rather
than multiplying canonical rate rows. Timestamps preserve the source instant;
viewers may display them in the client's chosen timezone.

## Quality checks

There are **29 dbt data tests**, declared in the staging schema YAML plus a singular
order-business-rules query. Built-in not-null and accepted-values tests are combined
with local generic tests for positive numbers and composite-key uniqueness.
No external dbt packages or additional dependencies are needed.

Orders require all exposed fields, valid country/currency values, positive IDs,
quantities/prices/totals, the composite key, quantity <= 5, country/currency
consistency, a matching quantity-times-price total, and the v2 currency-specific
price ranges (including whole-HUF multiples). Empty product/dataset/manifest values
also fail the business-rules test. Rates require all exposed fields, their complete
key, positive exact-decimal rates, provider ECB, base EUR, and quote HUF.

Tests return invalid rows and fail the build; they do not repair source data.
They do not enforce uniqueness of order_id alone. A failed dbt build can leave
created views in place; tests are not a transaction or access-control gate around
the entire project. Empty sources can pass these tests. Freshness/outage alerting,
revision handling, additional providers/pairs, and EUR-valued order marts are not
implemented by this milestone. See [dbt data tests](https://docs.getdbt.com/docs/build/data-tests)
for the supported generic and singular test behavior.

## Build and inspect

After reviewing the project, run from the repository root:

```powershell
.\scripts\dbt-dev.ps1 build
.\scripts\export-warehouse.ps1
```

Build writes the two staging views to the shared managed warehouse, through the
common lock. Export then produces an independent snapshot that includes them.
No image rebuild is needed for the development command. The subsequent Airflow automation deployment updated both baked project images;
scheduled code still requires reviewed image updates after local edits. Do not connect a
GUI to the live warehouse volume; open the exported retailflow.duckdb instead.
PowerShell execution-policy alternatives are in the [development guide](dbt-runtime.md#develop-models-from-vs-code).

```sql
select dataset_id, order_id, ordered_at, currency_code, total_minor
from staging.stg_orders
order by ordered_at desc
limit 100;

select rate_provider, rate_date, quote_units_per_base_unit
from staging.stg_exchange_rates;
```

Logs and compiled test queries are retained under ignored
`artifacts/dbt-dev/<invocation-id>/`. Use `target/run_results.json` for individual
model/test statuses. The canonical raw and first-manifest pointers remain visible;
full batch membership lineage continues to live in ops tables. Data ownership and
approved use remain the same synthetic retail/public-reference demonstration as
the raw layer; the views add no personal data or independent retention policy.

## Executed evidence

Three integration tests passed for staging/development commands. A valid fixture
with the same order ID in two datasets passed, views had the expected types,
12-place FX precision survived, and raw order totals were unchanged. Deliberately
corrupted unconstrained fixture sources caused failures for duplicate business
keys, null required values, nonpositive measures, accepted currencies, and order
business rules. These corrupt fixtures existed only in disposable volumes.
The separate developer-command tests still verified local-edit pickup and missing
volume rejection.

A second real build against a private copy of previously landed data produced
277 staging orders and one rate: both views and all 29 tests passed (31 dbt result
entries). The managed warehouse was untouched. Machine-readable evidence and logs
are referenced in ignored `artifacts/staging-model-validation.json`.
Ruff lint/format and whitespace checks passed. A secret scan of all five new
SQL/YAML files found no findings. No new dependency installation, dependency audit,
full regression run, container scan, Terraform apply, or Airflow task deployment
is claimed for this milestone. The next step is a reviewed managed build and,
after manual verification, Airflow integration using the isolated dbt environment.


## Managed build and snapshot verification ? 2026-11-02

The approved `dbt-dev.ps1 build` created both staging views under the shared lock.
All 29 tests passed: 31 successful model/test results, with no warnings, errors,
or skipped tests. Run results are saved under
`artifacts/dbt-dev/21c7f4c447844c64947be7370ed261b8/target/run_results.json`.

A fresh verified export was then queried independently. Both view definitions
remained queryable with the preserved retailflow catalog name. Counts matched
raw sources: 326 orders and one exchange rate. The rate column remained
DECIMAL(24,12). This is a snapshot count; enabled extraction schedules continue
to add raw data. Views reflect subsequent raw changes, but their dbt tests run
only on explicit builds until scheduling is implemented.

The exported file is:
`artifacts/warehouse-snapshots/20261102T165409548247Z-e586fc71f3eb4bafa7fc2935306f4ee7/retailflow.duckdb`.
Open this new path in DBeaver to see `staging.stg_orders` and
`staging.stg_exchange_rates`. Earlier snapshots do not acquire the new views.
Machine-readable evidence is in ignored `artifacts/staging-model-deployment.json`.
No Terraform apply, image replacement, raw data edit, or schedule change was needed.
Next milestone: reviewed Airflow execution of dbt with isolated dependencies and
the existing shared-lock protocol.
