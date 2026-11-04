# RetailFlow engineering instructions

- Inspect existing code, preserve working behavior, and deliver small working milestones.
- Keep the platform local-first and free of mandatory subscriptions or expiring trials.
  Never introduce paid dependencies silently or provision chargeable resources automatically.
- Azure and Snowflake are optional cloud targets. Keep infrastructure separate from logic;
  use environment configuration and document costs and unverified cloud examples.
- API configuration priority is process environment, working-directory `.env`,
  then defaults. Keep `.env` ignored and `.env.example` synchronized; restart to apply edits.
- Land orders as explicitly converted CSV and preserve original FX JSON bodies.
  Write checksum metadata last, verify downloaded bytes, and never overwrite different
  batch content. BlobStore accepts only loopback or approved Docker-local Azurite; cloud support requires
  a separately reviewed configuration. Never print emulator keys, Terraform state, or plan JSON.
- Use local Terraform Community CLI for infrastructure; Compose has been retired. Review
  saved plans before applying, back up landed blobs, preserve the existing volume,
  and verify blob checksums afterward. Ignore state, plans, and local import IDs;
  commit the provider lockfile. Sensitive Terraform variables still enter local state.
- Terraform owns the DuckDB image, runtime/container configuration, network attachment,
  and protected volume. Python owns database initialization, future schemas and loads.
  Keep the image context minimal and dependency pins/hashes aligned with uv.lock.
  Serialize future writers; never assume one container enforces single-process access.
- Warehouse loaders and health/read probes must use the common database FileLock
  (mode 0660). Scheduler writers access the protected volume through group 10001;
  preserve setgid directory mode 2770. Commit raw records, memberships, and batch
  ledger atomically; reject changed manifests and conflicting historical values.
- Warehouse backups must checkpoint and close DuckDB under the common FileLock,
  publish only complete checksum manifests, and restore only to a new target.
  Keep backup storage separate from the data volume; never auto-delete snapshots
  or promote a recovered file over the live warehouse without explicit approval.
- Terraform owns the isolated one-shot dbt image/container and protected artifact
  volume. Keep dbt dependencies in infra/dbt/uv.lock, matching shared DuckDB/filelock
  versions. Invoke dbt only through infra/dbt/run.py under the common warehouse
  lock; never mount the Docker socket into Airflow. Models and scheduling require
  their own reviewed milestone; runtime smoke checks must preserve raw tables.
  Use scripts/dbt-dev.ps1 for local edits: it mounts only the dbt project read-only,
  copies invocation artifacts to ignored artifacts/dbt-dev, checks existing storage/image,
  and invokes the same lock-aware entrypoint. Development builds use the shared
  managed warehouse; test models must run only in disposable storage.
- Airflow dbt tasks launch the shared wrapper from an immutable isolated venv,
  with a minimal credential-free child environment. Keep the shared warehouse
  lock for the full build, propagate dbt/test failures, and retain per-attempt
  artifacts in the protected Airflow logs volume; push only small summaries to XCom.
  Scheduled models are baked into the image and require reviewed image updates.
- The repository has one maintainer. The owner's explicit approval (including
  manually dispatching delivery) is sufficient; do not require another reviewer
  or prevent self-approval. CI uses read-only hosted runners and disposable data;
  local deployment still follows the approved saved-plan/backup workflow. Keep
  workflow actions pinned to verified commits and exclude secrets, state, data
  and runtime artifacts from delivery. Never attach public PR jobs to the local
  workstation or give CI the live Docker socket/credentials.
- Prepare local release reviews with `python -m scripts.release_review --archive
  <inner-source-zip> --commit <full-sha>` in the existing managed clean checkout.
  Keep its per-attempt backups, saved plans and reports under ignored artifacts;
  never apply automatically. Do not run legacy infrastructure commands concurrently.
  Package verification alone does not attest CI success. Pause/drain affected DAGs
  and preserve a locked warehouse snapshot before a separately approved changing apply.
- Staging models preserve raw rows, original minor units, and exact decimal rates.
  Orders are keyed by dataset_id plus order_id; rates by provider/base/quote/date.
  Keep quality checks visible as failing dbt tests rather than silently dropping
  invalid rows. Test deliberate corruption only in disposable storage.
- orders-v3 supports ten selected European currencies with versioned illustrative
  price profiles; retain orders-v2 contracts and source history. Keep the v3 orders
  checkpoint namespace separate and fail on unexpected API versions. FX extraction
  stages one immutable response per pair. EUR valuation requires exact-date/pair
  request lineage, excludes future rates and flags age >7 days; preserve missing
  rows with NULL EUR amounts. Use exact integer HALF_UP order-total rounding.
  Historical backfill is explicit, scope-bounded, checkpointed and separately
  reviewed before live writes. Keep source profiles, model tests and docs aligned.
- GUI database viewers must use independent verified snapshots. Export with
  scripts/export-warehouse.ps1, which invokes the deployed locked backup routine
  and verifies the transferred bundle before publishing a new local file. Preserve
  the retailflow.duckdb filename for catalog-qualified views; never overwrite a
  snapshot that may be open, or attach a desktop viewer to the live volume.
- Airflow images/DAGs use pinned Airflow constraints and PostgreSQL metadata with
  a non-superuser application role. Keep new schedules paused until deployment
  is verified. Persist source responses before upload, advance cursors only after
  verified manifests, and test task retries without changing batch content.
- Prefer modular Python with type hints, meaningful names, explicit error handling,
  structured logs without sensitive values, and readable parameterized SQL.
- Use Python 3.12 and the committed uv lockfile. Run Ruff, relevant automated tests,
  and security checks; report only checks actually executed and their results.
- Choose tests by changed behavior and dependencies; broaden for failures, shared
  runtime changes or unknown impact. Do not rerun the full suite for routine local
  edits. CI uses scripts/ci_select.py for path-based jobs/tests; missing diffs and
  unknown paths select full coverage. Manual CI and delivery always validate fully.
  Keep the source safety gate on every run. API image inputs are its package init,
  api.py and currencies.py; Airflow includes runtime modules but excludes the local
  infrastructure helper. Keep Docker copy rules and Terraform hashes aligned.
- Test new behavior, bug regressions, contracts, and meaningful integration boundaries.
- Data freshness belongs to dbt. Scheduled tasks invoke the lock-aware `pipeline`
  action: source freshness, tagged freshness tests, then build. Keep separate source/
  test/build artifacts and propagate any nonzero result. Current dataset version,
  seed and interval must match the API. Orders use 30 minutes; FX ingestion uses
  30 hours with per-pair checks and a seven-calendar-day publication allowance.
  Normal dbt build does not implicitly run source freshness; use the wrapper's
  freshness/pipeline actions. Test stale/empty/future data only in disposable storage.
- `python -m scripts.monitor` is an on-demand operational health report only;
  do not reimplement data freshness or poll-and-email failures there. HTTP endpoints
  remain loopback-only, proxies/redirects disabled, and warehouse probes read-only
  under the common 0660 FileLock. Keep reports under ignored artifacts/monitoring.
- Automatic email comes only from DAG-level on_failure_callback after a run fails,
  never task retries, success, recovery or timed reminders. Hash DAG/run identity,
  serialize delivery receipts on the protected Airflow logs volume and publish a
  receipt only after SMTP acceptance. Never email raw run IDs, exceptions or data.
  The DAG processor alone joins Mailpit's network for approved Docker-local SMTP;
  no socket mount or external provider credentials are allowed. Callback failures
  must be visible in processor logs; receipt/server deduplication is not an exactly-
  once or durable outbox guarantee. UI/CLI state edits do not trigger callbacks.
- Mailpit keeps loopback SMTP 1025/UI 8025, protected persistent storage, fixed
  operator@retailflow.test recipient and relay disabled. Enable Message-ID duplicate
  suppression. No automatic inbox deletion; cleanup is an explicit owner action.
  scripts.monitor_mail requires --test-email or --dry-run; old mail-state.json is
  retained but unused. Never add a periodic email schedule or reminders silently.
- Never hardcode or commit credentials. Apply least privilege, safe configuration,
  dependency/container scanning where applicable, and test security enforcement.
- Document threat models and significant architecture/security decisions.
- Record dataset ownership, classification, lineage, contracts, quality checks,
  approved uses, retention, and deletion requirements. Use synthetic personal data only.
- Distinguish implemented, tested, simulated, and planned controls. Never claim
  production deployment, RBAC, privacy enforcement, or legal compliance without evidence.
- Keep README and docs synchronized, especially docs/architecture.md,
  docs/roadmap.md, docs/security.md, docs/governance.md, and docs/cost-policy.md
  as those documents are introduced in Milestone 0.
- Update this file when architecture, commands, security/governance rules, or cost
  assumptions change. Check consistency with the architecture and roadmap at each milestone.
- Explain substantial architectural changes and trade-offs before implementation.
  Clarify the next step with the user before acting; the user uses "y" for yes.
- Do not commit, push, deploy, or provision cloud infrastructure without explicit approval.
- After each milestone report changed files, executed tests, limitations, demonstrated
  engineering skills, and the recommended next milestone.
