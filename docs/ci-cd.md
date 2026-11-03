# Single-maintainer CI and continuous delivery

Status: hosted CI and the first manual delivery succeeded for commit e1d8533.
[Delivery run 41597830238](https://github.com/ungvari-tomi/tomi-platform/actions/runs/41597830238)
passed all verification jobs and produced a source package. Its ZIP checksum and
all 100 source files were checked against Git. A subsequent local plan showed no
changes and verified 794 backed-up blobs. Earlier test-import and PostgreSQL
readiness failures were corrected before this successful delivery. The repository is public
and the owner has admin/push access. There is one maintainer: the owner's approval
is sufficient. No second reviewer, required approval from another account, protected
environment reviewer, or prevent-self-review setting is introduced.

## Continuous integration

`.github/workflows/ci.yml` runs on main pushes, pull requests, and manual dispatch;
it can also be called by delivery. Jobs use read-only contents permission,
checkout without persisted credentials, pinned action commits, Python 3.12.16,
uv 0.13.6 and Terraform 1.17.1. Cache uploads are disabled. Repeated runs for the
same workflow/ref cancel superseded work. There are no workstation credentials,
cloud credentials, registry credentials or live Terraform state in CI.

- An always-on Ubuntu job checks source secrets/operational files and selects
  coverage from Git diffs, including deleted/renamed paths. It fetches history
  without persisting credentials and fails open to full coverage on missing diffs.
- Selected Windows and Ubuntu jobs check Ruff lint/format, Bandit and relevant
  tests. Optional Docker tests are skipped in these jobs.
- Ubuntu validates GitHub workflow syntax with checksum-pinned actionlint 1.7.12,
  Terraform formatting/schema using backend-disabled init and the readonly provider
  lock, then audits both locked runtime dependency exports with pip-audit. These
  steps run only when their selected scope requires them. No plan
  or apply is run against the Windows Docker configuration.
- Ubuntu builds the five validation images, generates a masked emulator-only key,
  starts disposable Azurite bound to loopback, and runs selected opt-in tests. The
  tests use UUID-named temporary containers/volumes, mocked external FX responses,
  and synthetic data. They do not connect to the laptop or a cloud storage account.
  Ubuntu's PowerShell (pwsh) executes developer-command tests. The development
  helper now writes to an anonymous Docker artifact volume and copies it to the
  host before cleanup, avoiding Linux UID conflicts with writable bind mounts.

The secret gate inventories git-tracked source and fails on new detected secrets,
.env files, Terraform state/plans/local import IDs, database files, generated
outputs, runtime directories, or source symlinks. Only .env.example and the public
provider lock remain allowed configuration examples. Reports contain file/line/type,
never secret values. Narrow inline allowlists cover documented public identifiers
and mount paths; there is no broad baseline accepting new findings. This is a
heuristic scanner, not proof that every possible secret is detected.

### Relevant tests after a change

`scripts/ci_select.py` applies this conservative policy to pushes and pull requests:

| Change | Coverage |
| --- | --- |
| README, AGENTS or docs only | Always-on source safety gate; skip runtime tests |
| Local infrastructure helper | Infrastructure/release-review unit tests and Terraform checks |
| Release/package helpers | Related unit tests; skip image builds |
| Local monitoring scripts | Monitoring unit/HTTP/locked-database tests; skip image builds |
| Local email sender | SMTP/deduplication tests; opt-in Mailpit test validates capture/persistence |
| Local Terraform files | Infrastructure unit tests, fmt/schema and real image-hash boundary test |
| Test file only | That test module; enable Docker when its opt-in markers require it |
| Shared runtime, build inputs, locks, workflows, fixtures or unknown paths | Full coverage |
| Manual CI or reusable delivery | Full coverage regardless of paths |

The test runner accepts only explicit test-module paths or the whole tests directory,
never shell fragments/options from Git filenames. CI always prints its selection.
The source safety gate remains unconditional. Delivery explicitly requests full
validation through the reusable workflow input; this cannot inherit a docs-only
skip. Local work follows the same principle: run affected tests first and broaden
only for new failures, shared effects or uncertainty.

The selection milestone passed hosted CI and
[delivery validation](https://github.com/ungvari-tomi/tomi-platform/actions/runs/41628948870)
for fc998f9. Full delivery validation remained enabled. Its reviewed local saved
plan was subsequently applied with owner approval; see [deployment checks](terraform.md#application-image-inputs).

## Continuous delivery and your approval

In GitHub **Actions > Prepare delivery > Run workflow**, select **main**. Your
manual dispatch is the release approval. No other person's approval is requested.
The workflow reruns CI for that exact commit. Only after all verification jobs pass
does it package a delivery artifact named with github.sha. No environment reviewers
are required by these workflows; existing account/branch policies remain separate
GitHub settings. You can choose optional required CI status checks without adding
required pull-request reviewers.

The package is a small source ZIP, SHA-256 checksum and internal per-file manifest
containing the commit identity and content hashes. It includes only committed source
and requires a clean worktree and key platform files. Its ZIP metadata is fixed for
reproducible local packaging. Source hashes identify contents; there is no signing,
build attestation, SBOM, image-registry publishing or production compliance claim.
The packaging helper itself checks clean source; the workflow's needs gate supplies
the CI requirement. Large Docker images, data, credentials, state and caches are not
uploaded. Both the source delivery and the JUnit summary expire after one day.

This is continuous delivery with local promotion: Docker Desktop and the persistent
warehouse run on your Windows computer, so the release is handed to the existing
reviewed local Terraform process. GitHub does not have access to that Docker daemon.
Automatic remote deployment to the workstation is a separate design requiring an
explicitly configured trusted runner or transport; it is not implied by adding CI.

## Promote locally

1. Run Prepare delivery on main and verify all checks pass. Download its source
   artifact and verify the ZIP hash with PowerShell Get-FileHash -Algorithm SHA256
   against the supplied .sha256 file. Review release.json for the source commit.
2. Promote that same approved commit in the **existing managed checkout**, preserving
   ignored .env, Terraform state/plan files, data, tool bootstrap and volumes. Resolve
   or preserve uncommitted work before a checkout; do not overwrite it automatically.
   The archive is for delivery/review, not a replacement Terraform state directory.
3. Sync the lockfile, prepare/inspect the local Terraform plan, pause/drain affected
   DAGs and preserve a verified independent warehouse snapshot before applying.
   Use the existing infrastructure helper with its secret injection and blob backup
   verification. Apply the saved reviewed plan with your approval.
4. Verify health, controlled DAG runs, dbt tests/artifacts, data preservation and a
   no-change Terraform plan, then restore schedules. Export a new DBeaver snapshot.

Never run a fresh Terraform apply directly from an extracted package with no local
state: named container/volume collisions and loss of management continuity can
result. No new live deployment was performed in this CI/CD milestone.

## Repeatable local release review

The new local command combines source verification, Terraform validation, blob
backup, saved-plan preparation and backup checksum verification. It never applies
a plan, extracts source, checks out a commit, pauses schedules or changes running
containers. Use the **inner source ZIP** beside its `.sha256` file, after downloading
the GitHub artifact. Run from the existing managed checkout with its state and .env.

```powershell
$releaseCommit = git rev-parse HEAD # Confirm it matches the successful delivery run.
$releaseArchive = "artifacts/delivery-41597830238/retailflow-$releaseCommit.zip"
.\.venv\Scripts\python.exe -m scripts.release_review `
  --archive $releaseArchive --commit $releaseCommit
```

The example commit must match the checkout's HEAD. Full review requires a clean
committed worktree, including untracked source; ignored local runtime files remain
in place. Use `--verify-only` to check package bytes against HEAD without requiring
a clean worktree or accessing Docker/Terraform. SHA checks identify content; this
command does not independently authenticate GitHub CI results. Review the successful
delivery run for the same commit before invoking it.

Each attempt gets a unique ignored `artifacts/release-reviews/<id>/` directory with
`blobs/`, `review.tfplan`, `plan-summary.json` and a final `review.json`. Plan/state
contents remain private; only safe summaries and hashes belong in review reports.
The Terraform helper's `--review-directory` keeps these backups and plans separate
from previous attempts and the legacy migration plan. Partial attempts are retained,
with no successful review marker. Nothing is automatically deleted. Keep these local
files protected: saved plans contain secrets and blob backups contain synthetic data.

The command serializes its own invocations with a local FileLock. Do not concurrently
run legacy infrastructure commands or edit the checkout. It rechecks HEAD/worktree
after planning, but the saved plan and live data must still be reviewed immediately
before a separately approved apply. Snapshot the warehouse under its common lock
and pause/drain affected DAGs before any changing deployment; this review command
backs up blobs only. A no-change plan needs no apply.

This command is included in the owner-approved release-review source milestone.
Package checks have been exercised against real deliveries, and the combined
command completed end to end for c46bcae and fc998f9 in the clean managed checkout.
The fc998f9 review verified 105 source files and 840 backed-up blobs before the
separately approved apply. A subsequent review reported no Terraform drift.
Local validation on 2026-11-03: 143 tests passed and 15 opt-in Docker tests were
skipped. Ruff lint/format, Bandit and the 102-file source safety scan passed. Tests
cover package tampering, unexpected entries, wrong commits, dirty checkouts,
separate attempts, failed plan/backup stages, private-output suppression and
preservation of the legacy saved plan. No live apply or new hosted run was performed.

## Activation and costs

The platform source and workflows are committed and published. The package helper
rejects a dirty/uncommitted worktree or missing platform source. Subsequent commits
and pushes still require the owner's approval. Inspect hosted results and correct
runner-specific issues before treating GitHub execution as verified.

[GitHub's billing documentation](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
states that standard GitHub-hosted runners are free for public repositories. This
setup uses standard runners, short retention and small artifacts, with bounded job
timeouts and no mandatory paid subscription. Private-repository quotas or future
billing policy changes need a fresh cost review. No account spending settings were
changed or verified. Keep paid overage disabled if you later change repository plans.

## Local commands and validation

```powershell
.\.venv\Scripts\python.exe -m scripts.ci_safety --include-untracked
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

The include-untracked flag is for preparing the initial commit locally; CI scans
tracked source. release_bundle is invoked only from a clean committed checkout:

```powershell
.\.venv\Scripts\python.exe -m scripts.release_bundle
```

Local verification completed on 2026-11-02:

- Full opt-in suite: 142 tests passed, including disposable Azurite, warehouse,
  Airflow-image and dbt-image integration checks.
- Ruff lint/format, Bandit, actionlint and Git whitespace checks passed. The source
  safety gate scanned 100 source files with no findings.
- Terraform formatting and Windows validation passed. Backend-disabled init with
  the readonly lockfile and validation also passed inside Linux Terraform 1.17.1.
  The provider lock now includes both Windows and Linux package checksums; its
  Docker provider version remains 4.6.0.
- Package tests verify file checksums, deterministic output, secret exclusions,
  dirty-checkout rejection and refusal to overwrite an existing delivery.

Earlier hosted test-import and PostgreSQL readiness failures were corrected and
verified in successful CI/delivery runs. The owner-approved fc998f9 deployment
completed with verified backups, preserved historical warehouse records, healthy
services, six first-attempt successful pipeline tasks, two successful dbt builds
and no Terraform drift. Both schedules are enabled. No cloud resources were
provisioned. Deployment evidence is retained in ignored
`artifacts/deployment-fc998f9/deployment.json`. These documentation updates do not
change runtime image inputs.
