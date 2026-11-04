# SQL capitalization and linting

SQLFluff is a development dependency pinned by the root `uv.lock`. It runs locally
and in CI using the DuckDB dialect and static Jinja templating. No subscription,
database connection, warehouse lock or runtime credentials are required.

```powershell
uv sync --locked
uv run --locked sqlfluff lint infra/dbt/project
uv run --locked sqlfluff fix infra/dbt/project
```

The `.sqlfluff` rules enforce uppercase SQL keywords (CP01), function names (CP03),
NULL/boolean literals (CP04), and data types (CP05). Identifier capitalization is
not enforced: table names, column names and aliases retain their existing case.
Quoted strings and quoted identifiers remain unchanged, as does Jinja syntax.

```sql
SELECT customer_token, COUNT(*) AS order_count
FROM staging.customer_orders
WHERE country = 'HU'
GROUP BY customer_token
```

Jinja uses SQLFluff's dbt built-ins and a local `env_var` mock that returns the
supplied default without reading the environment. Undefined template functions
and SQL parse errors fail linting. Generated `target`, `logs` and `dbt_packages`
directories are excluded by `.sqlfluffignore`.

This is static linting, not a dbt compile or data-quality check. Uninvoked macro
bodies and dynamically assembled SQL still require review and dbt validation.
Embedded Python SQL and YAML freshness filters follow the same capitalization
style but are outside SQLFluff's `.sql` file scan. Negative linter regression
fixtures intentionally contain lowercase SQL to prove violations are detected.

The GitHub Actions cross-platform checks job runs SQLFluff alongside Ruff and
fails on violations. The delivery workflow reuses those checks with full
validation and waits for them before packaging source. Editing only SQLFluff
configuration selects focused style tests without rebuilding Docker images.

Capitalization edits do change baked source hashes. Refresh any saved deployment
plan before applying these updated dbt/runtime images; existing review plans
predating this change do not describe the current source. No deployment is
performed by running the linter or formatter.

Local validation passed the project SQL lint, 70 focused tests and a separate
real dbt parse check; one opt-in test was skipped. Ruff, Bandit, workflow syntax
validation, source-secret checks and the locked dependency audit passed. Existing
dependency versions were preserved; the new packages are development tooling.
All changed `.sql` files were checked against Git to confirm only letter case
changed. These checks do not attest a new hosted CI run or live deployment.

SQLFluff's [configuration reference](https://docs.sqlfluff.com/en/stable/configuration/)
describes the Jinja/dbt built-ins and capitalization settings used here.
