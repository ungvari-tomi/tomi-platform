# Local Terraform migration

[Ten-currency generation and EUR valuation](european-currencies.md) are deployed; the versioned migration and live pipelines are verified.

[Airflow dbt automation](airflow-dbt.md) is deployed; both live three-task verification runs passed.

A [Terraform-managed dbt Core runtime](dbt-runtime.md) is deployed; its managed source checks passed.

[Daily warehouse backups and recovery](warehouse-backup.md) are deployed, with verified startup backup and recovery.

Applied on 2026-11-02: [warehouse loading](warehouse-loading.md), with one
permissions initializer addition and nine image/container replacements. No data
volume is deleted or replaced. The extraction/loading stack is running, and the post-apply plan has no changes.

The Azurite migration and subsequent **DuckDB plus Airflow deployment** are
complete. The 15-resource addition preserved Azurite data and the post-apply plan
has no changes. See [orchestration](orchestration.md) and
[DuckDB runtime](duckdb-runtime.md). Both DAGs are now enabled and verified. Always review future
plan summaries before applying.

Status: migration applied and verified on 2026-10-31. Terraform now manages
Azurite. The existing volume/network were imported and retained; only the
container was replaced. All seven pre-migration backup blobs were verified after
apply. A subsequent live plan reported no changes. Compose has been retired.

Terraform Community CLI runs locally with local state. No HCP account, credit
card, subscription, remote backend, or Azure resources are used. The only provider
is `kreuzwerker/docker` 4.6.0. Terraform CLI 1.17.1 is pinned. Source configuration
lives in `infra/local`; the signed provider checksums are committed in
`infra/local/.terraform.lock.hcl`.

## Reviewed migration approach

The plan adopts these existing resources by their discovered local IDs:

- `tomi-platform_azurite-data`: retains the data volume, protected by `prevent_destroy`.
- `tomi-platform_default`: retains the network, protected by `prevent_destroy`.
- `tomi-platform-azurite-1`: imports then replaces the container, reattaching the
  same `/data` volume and retaining loopback port 10000 and the local key.

The provider does not populate environment variables on import and only imports
port mappings when already configured in its state. Consequently, managing those
settings explicitly requires container replacement here. Hiding those differences
with `ignore_changes` would prevent meaningful management of critical settings.
This plan instead accepts a brief service interruption while preserving storage.
See the [provider implementation](https://github.com/kreuzwerker/terraform-provider-docker/blob/v4.6.0/internal/provider/resource_docker_container_funcs.go).

Legacy Compose labels and the network's auto-assigned IPAM configuration are
ignored to avoid unrelated replacements. Volume/network names remain unchanged
for compatibility. The container image stays pinned to Azurite 3.37.0; the Docker
provider can locate/pull that image when creating the container. No image or data
volume deletion is requested by this migration.

## Commands from VS Code PowerShell

Docker Desktop's Linux engine must be running. The checked local CLI is available
at `.bootstrap/terraform/terraform.exe`. On a fresh machine, install Terraform
1.17.1 on PATH; the Python helper supports either location. The Windows archive
was verified against HashiCorp's published SHA-256 over HTTPS; a separate GPG
verification of the archive checksum file was not performed.

```powershell
.\.venv\Scripts\python.exe -m retailflow.infrastructure init
.\.venv\Scripts\python.exe -m retailflow.infrastructure validate
.\.venv\Scripts\python.exe -m retailflow.infrastructure plan
```

The helper reads the ignored `.env` and injects the emulator key through a
sensitive Terraform variable. It discovers IDs without printing container
environment variables and writes ignored `imports.local.tf`. Fresh installations
without these resources generate a creation plan instead. Review that plan
separately; the current installation's import/replacement result does not imply
that fresh creation has been tested.

Before planning against an existing container, every current blob is backed up
under ignored `artifacts/terraform-migration/`, keyed by its SHA-256. The inventory
records each original blob name, checksum, and size. This is an application-level
backup of the `landing` container, not an atomic filesystem snapshot. Pause source
landing jobs during migration; do not delete the original volume. The helper
verifies each saved blob against the source immediately before apply and again
afterward. It rejects plans that delete the volume/network, remove the container
without replacement, or reference resources outside the local allowlist. The
allowlist now also includes the DuckDB image/container/volume; destruction of
either persistent volume is refused. Image/container replacements are reported
explicitly in the plan summary.

Only after explicitly approving the concrete saved plan:

```powershell
.\.venv\Scripts\python.exe -m retailflow.infrastructure apply
.\.venv\Scripts\python.exe -m retailflow.infrastructure plan
.\.venv\Scripts\python.exe -m retailflow.infrastructure verify
.\.venv\Scripts\python.exe -m retailflow.blob_storage check
```

`apply` executes the saved plan without another Terraform interactive prompt; the
user's explicit request to run that command is the approval boundary. A stale
Terraform state rejects an old plan. Regenerate and review the plan after changing
configuration, keys, or resource state. After migration, another plan reported no
imports or runtime changes.
Existing harmless import blocks may remain but are no-ops after resources enter state.

Do not run `terraform destroy` as a shutdown mechanism. For a temporary stop use
`docker stop tomi-platform-azurite-1`; start it with `docker start tomi-platform-azurite-1`.
The configuration expects the service to be running, so a later Terraform apply
may start it again. Do not run Compose up/down against these resources after
handover. The old root Compose file has been removed after successful migration.

## State and failure handling

State and saved plans can contain the emulator key in plaintext even though the
variable is sensitive. `.gitignore` excludes state/backups, plans, generated
configuration, local import IDs, and `.terraform/`. Never publish `terraform show
-json`, raw debug logs, `.env`, or any saved plan/state. The helper disables inherited
Terraform debug logging and injected CLI argument variables. Local OS permissions
remain the boundary; encryption at rest is not added here.

If recreation fails, keep the original named volume and existing key, inspect
the error, then regenerate/review a Terraform plan to recreate the container using
that volume. Do not prune volumes, switch keys, or run Compose concurrently.
The blob backup and volume remain available for investigation. Volume and network
`prevent_destroy` guards apply only while their configuration remains present;
they are not storage WORM or a substitute for backups.

Configuration formatting, schema validation, live planning, migration apply,
post-apply no-drift, and post-migration integrity checks have been executed.
No cloud deployment or infrastructure security
scan has been performed. Terraform uses its current source-available license;
the project's MIT license does not relicense Terraform or its providers.

Verification on 2026-10-31: Terraform formatting and validation passed. The saved
live plan reports three imports and replacement of only the container, with no
volume/network deletion. Seven pre-plan blobs were backed up and verified against
the source. All 57 Python tests passed with live Azurite enabled; Ruff and Bandit
passed. Bandit's subprocess import/call checks have narrow, documented suppressions
for the helper's fixed CLI argument-list execution without a shell. The approved
apply completed, all seven original backup blobs matched afterward, and a new
blob upload/download check passed. The follow-up plan reported no imports,
creates, updates, replacements, or deletes.

## Application image inputs

The orders API copies and hashes only `retailflow/__init__.py`, `api.py` and
`currencies.py`. Airflow copies/hashes runtime Python modules with the local
`infrastructure.py` helper excluded by both Docker ignore rules and Terraform.
Dockerfiles, requirements, DAGs, dbt files and other existing image-specific
triggers remain independently tracked. Future imports must update the API copy
list and hash inputs together. Unknown Airflow module additions remain included.

The narrower hashes and Docker context require one migration rebuild of the two
application images. A saved local plan proposes those images and five dependent
containers, with no volume/network replacement. This plan was reviewed but not
applied. Afterwards, helper-only changes no longer invalidate either application
hash; warehouse changes affect Airflow only, while currency changes affect both.
`tests/test_image_inputs.py` evaluates the actual Terraform expressions in a
provider-free disposable tree to verify those boundaries.

Focused validation for this milestone: CI-selection/infrastructure/hash tests,
two rebuilt-image smoke tests, workflow lint, Terraform fmt/schema and a saved
local plan. Its blob backup verified 810 objects. No full-suite rerun or live
deployment was performed for this change.
