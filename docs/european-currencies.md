# European currencies and date-aware EUR valuation

Status: deployed locally through Terraform on 2026-11-02. The managed API reports
orders-v3, daily FX extraction supports nine ECB pairs, and both Airflow pipelines
successfully built three models and passed all 37 tests. Existing v2 rows and its
checkpoint were preserved. Both schedules remain enabled.

## Synthetic orders-v3

The new generator supports EUR, HUF, PLN, CZK, RON, GBP, CHF, SEK, NOK and DKK.
It remains deterministic by order ID, seed, interval and version. Price ranges
are illustrative retail assumptions, independent of live FX prices:

| Country | Currency | Unit price range in major local units |
|---|---|---|
| HU | HUF | 2000?80000 |
| DE | EUR | 5?200 |
| FR | EUR | 5?200 |
| PL | PLN | 20?900 |
| CZ | CZK | 120?5000 |
| RO | RON | 25?1000 |
| GB | GBP | 4?180 |
| CH | CHF | 4.5?190 |
| SE | SEK | 50?2300 |
| NO | NOK | 55?2400 |
| DK | DKK | 35?1500 |

Prices and totals are stored as integer minor units, with two accounting decimal
places for every supported currency. HUF, CZK, SEK and NOK samples use whole local
units; other profiles allow two-decimal prices. Quantity remains 1?5. More units
in a lower-valued currency do not imply a larger economic basket. Small response
pages may not contain every currency; the 100-row validation sample contained all
ten. The API response schema remains /v1/orders, with dataset_version=orders-v3.

Settings and Terraform now accept RETAILFLOW_DATASET_VERSION (orders-v2 or
orders-v3), defaulting to v3 in updated code. The new field is documented in
.env.example; existing .env files were not edited. The loader and landing client
accept both historical v2 and new v3 batches. v2 generation and price contracts
remain reproducible; new countries cannot be mislabeled as a v2 batch. Dataset
identity includes the version, so the same numeric ID in two versions represents
two synthetic scenarios, not a duplicate to overwrite. Filter dataset_id when
analysing a single scenario; do not mistake combined version totals for one
continuous real-world sales stream.

The scheduled v2 state retains its original location. v3 uses
/opt/airflow/state/orders-v3/, leaving the v2 cursor, saved runs and response files
untouched. A mismatched API version fails before advancing a cursor. The first
new-version run retains the existing bootstrap policy: latest 20 orders, then
incremental ingestion. This does not regenerate all historical slots as v3 or
rewrite old raw data. Other seed/interval changes still require deliberate review.

## European FX ingestion

The daily FX extraction task fetches one exact original JSON response per non-EUR
quote, always base EUR and provider ECB. There are nine normal requests per run;
transient retries remain bounded at three attempts per pair. Response staging and
batch IDs include the quote, preserving HUF's legacy keys and isolating the other
eight currencies. On retry, completed captured responses are reused. The task
returns only the small manifest list, then raw loading and dbt_build follow.
Its extraction timeout is raised to 20 minutes for the bounded nine-pair worst
case; source schedules, warehouse locks and dbt subprocess limits are unchanged.

The public [Frankfurter API](https://frankfurter.dev/) supplied all nine ECB pairs
in live verification for 2026-11-02. Original responses and observation metadata
were saved under ignored artifacts/european-fx-validation/. No API key or paid
service was introduced. The HUF helper remains available, and the generic CLI is:

```powershell
.\.venv\Scripts\python.exe -m retailflow.exchange_rates --date 2026-11-02 --quote PLN
```

The landing CLI also supports --quote, retaining JSON bytes and checksum metadata.
Rates remain canonical per provider/base/quote/publication date, with separate
request-date memberships. Different requests can reuse the same weekend rate.
Revised conflicting canonical values still fail rather than silently overwrite.

## Coverage report and explicit bounded backfill

The inspected v2 snapshot had order dates 2026-10-31 through 2026-11-02, with
verified date-specific requests for each. Friday's published rate covered the
weekend; Monday had its own newer publication. It needed no historical API calls.
The report helper works on any initialized warehouse file under the shared lock:

```powershell
.\.venv\Scripts\python.exe -m retailflow.fx_coverage --database PATH_TO_COPY\retailflow.duckdb
```

A currency/date is covered only when request lineage exists for that exact UTC
order date and pair, linked to a loaded canonical ECB rate on or before the date.
Merely having a nearby rate is not proof that it is the provider's date-specific
answer. A publication older than seven calendar days is stale. The report does
not create a missing database, make network calls, or write to the warehouse.

Backfill is an explicit mutation, not an automatic new Airflow task. After review:

```powershell
# After deployment, inside the already configured scheduler runtime:
docker exec retailflow-airflow-scheduler python -m retailflow.fx_coverage --database /warehouse/retailflow.duckdb --backfill --state-directory /opt/airflow/state/fx-history --max-pairs 31
```

Each requested date/currency pair is counted against max-pairs (default 31,
allowed 1?366). Too many pairs, unsupported currencies or future request dates
are rejected before API calls. The helper reuses capture-before-upload staging,
lands original JSON plus metadata, and loads only verified complete manifests.
Each pair commits independently; earlier valid pairs survive a later failure.
Missing acknowledgements and retries reuse identical responses. Existing covered
pairs are skipped without API calls. Persistent state is required and must not
be casually deleted: it freezes response bytes across retries. Re-run with the
same state after transient failure. A stale upstream answer can remain stale;
inspect the after report rather than assume all gaps were repaired.

## EUR model and exact rounding

`staging.orders_eur` is a view over stg_orders and stg_exchange_rates plus request
lineage. Every original order and composite key remains visible. The order date
is derived explicitly in UTC, independent of a viewer's timezone. The newest
eligible publication linked to an exact-date/pair request is selected; future
publication dates are excluded. Repeated request memberships do not multiply rows.
For same-publication duplicates, a deterministic manifest path retains traceability.

EUR amounts pass through unchanged. Other selected currencies use local units
per EUR; since all selected minor-unit scales are two, total_minor/rate gives EUR
cents. The complete order is rounded once, HALF_UP, not the per-unit price.
DuckDB ordinary DECIMAL division returns floating point; this implementation uses
scaled rate integers and HUGEINT // arithmetic to avoid precision loss, then
converts cents to DECIMAL(24,2) using decimal multiplication. See the
[DuckDB numeric types reference](https://duckdb.org/docs/current/sql/data_types/numeric).
Intermediate integer arithmetic fits the existing BIGINT/DECIMAL rate domain.
A converted cent result outside BIGINT range fails explicitly rather than truncating.
Refund/negative-order semantics are outside the positive-order contract.

| conversion_status | EUR values |
|---|---|
| identity | Original EUR total, unchanged |
| converted | Verified date/pair with publication age 0?7 days |
| missing_rate | NULL, with the original order retained |
| stale_rate | NULL, with candidate date/provenance retained |
| unsupported_currency | NULL; defensive state for a future unknown code |

Outputs include order_date, eur_total_minor (BIGINT EUR cents), total_eur
(DECIMAL(24,2)), currency_units_per_eur, fx_rate_date, fx_age_days,
fx_rate_manifest_path and fx_request_manifest_path. Existing order fields and
original currency amounts remain. This is daily reference valuation, not a rate
known at the intraday purchase time: a same-date ECB rate may be published later
that day. Before the daily FX task supplies a new day's requests, some orders can
have missing_rate status. That is visible and does not stop staging quality tests.
No missing/stale amount is replaced with zero. Always report coverage counts with
EUR totals; aggregate only identity/converted rows and do not mix generator scenarios
unintentionally. The seven-day limit is an explicit project policy, not an upstream
freshness guarantee.

The new model adds eight tests: required identifiers/date/status, accepted statuses,
composite uniqueness, row preservation, and amount/status/date consistency. Combined
with the original 29 tests, the project has **37 tests and three models**. Missing
and stale statuses are permitted and visible; these tests do not guarantee complete
coverage or alert externally. Backfill and revision handling remain explicit actions.

## Executed validation and pending migration

Unit tests verify all ten currencies and eleven countries, unchanged v2 generation,
side-by-side loading of overlapping IDs in v2/v3, every supported FX pair, separate
pair response caches, replay without extra API calls, v3 checkpoint isolation,
and bounded retry-safe backfill. Real dbt tests cover UTC date boundaries, weekends,
future-rate exclusion, duplicate request memberships, missing requests, seven/eight
day age boundaries and exact half-cent rounding (including a near-tie that binary
floating point would round incorrectly). EUR identity and all nine foreign pairs
were checked against the Decimal oracle.

An isolated end-to-end sample generated 100 v3 orders across all ten currencies,
landed CSV, loaded the nine original live ECB JSON responses with request lineage,
and built all three models. All 37 dbt tests passed. Every output amount matched
the Python Decimal oracle, all required pairs were covered, and the managed
warehouse was untouched. Evidence is in ignored artifacts/european-e2e-validation.json.
The validation sample and live API checks are not deployed production controls.

The approved Terraform plan replaced nine images/containers (API, Airflow and dbt),
creates/deletes no standalone resources, and preserves all data, metadata, state,
log, backup and artifact volumes and the Docker network. No warehouse schema
migration or old data rewrite is needed. The approved apply completed on 2026-11-02.
Pause/drain both DAGs, preserve a verified independent snapshot, apply serially,
verify source API reports v3 and v2 state is retained, run all-pair FX extraction
before the initial v3 orders build, inspect exact-date coverage and backfill only
required historical pairs within the reviewed limit, verify the three models and
37 tests, confirm no drift, and restore schedules. The full regression suite passed **130 tests in 252.83 seconds**, with all
Azurite/Airflow/DuckDB/dbt opt-ins enabled. After diagnostic wording cleanup,
39 FX/loader checks passed again and the final API/Airflow validation images were
rebuilt. Ruff lint/format, Bandit, Terraform formatting/validation and whitespace
checks passed; a secret scan of ten relevant implementation/model files found
no findings. Dependencies were unchanged, so no new dependency audit is claimed.
No full container vulnerability scan was executed. The final saved plan matches
the current files and is ready for review.


## Managed migration evidence ? 2026-11-02

Both DAGs were paused and no active/queued runs remained. An independent
checkpointed warehouse snapshot verified 335 orders and two canonical HUF rates
before replacement. Its path and the pre-migration checkpoint digest are recorded
in ignored artifacts/european-predeployment.json. The reviewed plan was applied
serially with the existing helper, and all 609 backed-up source blobs verified
unchanged. The updated standalone dbt check exited 0; the post-apply Terraform
plan reported no changes. No persistent volume or raw schema was replaced.

The managed API served a 100-row v3 sample containing all ten currencies. The
controlled FX run european_fx_deployment_20261102 completed before orders were
enabled. Then european_orders_deployment_20261102 succeeded through extraction,
loading and dbt_build. All six tasks passed on attempt 1, with three models and
37 tests recorded in each dbt run-results artifact. Scheduled tasks retain their
normal cadence; neither source was accelerated for this migration.

v3 bootstrapped its configured latest 20 rows, giving 355 total orders alongside
the retained 335 v2 rows. The initial small v3 batch contained nine currencies
(no PLN order yet), which is expected: a small deterministic sample need not
contain every supported category. The 100-row API sample included PLN. The
warehouse contains all nine foreign FX pairs, so new PLN orders can be valued
when their dates have verified request coverage.

The v2 checkpoint retained SHA-256
55cc1028f6769e296e22d1e4e36aca38ad94bcdbc715a3fa8cfa29d2801bc0be.
A complete ordered digest of every v2 order field and first-manifest pointer
matched the independent pre-deployment snapshot. The new orders-v3 checkpoint
has version v3, seed 42 and interval 600. Bounded coverage reconciliation found
all ten required currency/date pairs covered and attempted zero extra API calls.
Every one of the 355 EUR amounts matched the Decimal oracle; 228 EUR orders
passed through unchanged and 127 foreign-currency orders were converted. There
were no missing/stale rows in this verification snapshot. These are point-in-time
counts; later dates can be incomplete until their FX requests are loaded.

The new verified DBeaver file is:
artifacts/warehouse-snapshots/20261102T182900384438Z-9ff7b71543cc4376b0eda82111fe6ec1/retailflow.duckdb.
It includes staging.orders_eur with queryable preserved catalog references.
Detailed task states, coverage, digests, counts and dbt artifact paths are in
ignored artifacts/european-deployment.json. Existing raw originals and snapshots
were retained. The 130-test suite passed before deployment; no application code
changed during apply. No container vulnerability scanning, live corruption
injection, or historical data rewrite is claimed. Recommended next milestone:
EUR sales summaries with dataset filters and explicit coverage counts.
