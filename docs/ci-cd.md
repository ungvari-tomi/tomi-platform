# Single-maintainer CI and continuous delivery

Status: workflows and local safety helpers are published on GitHub. The first run
passed infrastructure/dependency checks but failed test collection because the
pytest executable did not include the source checkout on Python's import path.
Both test jobs now invoke `python -m pytest`, matching local validation; the
corrected hosted run remains to be verified. The repository is public
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

- Windows and Ubuntu check Ruff lint/format, Bandit, source secret/operational-file
  safety and the normal test suite. Optional Docker tests are skipped in these jobs.
- Ubuntu validates GitHub workflow syntax with checksum-pinned actionlint 1.7.12,
  Terraform formatting/schema using backend-disabled init and the readonly provider
  lock, then audits both locked runtime dependency exports with pip-audit. No plan
  or apply is run against the Windows Docker configuration.
- Ubuntu builds the four validation images, generates a masked emulator-only key,
  starts disposable Azurite bound to loopback, and runs the full opt-in suite. The
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

The first GitHub run passed infrastructure/dependency checks. Its Windows, Ubuntu
and integration jobs failed during test collection; the invocation correction is
described above. Local package behavior was tested with disposable fixtures; a
hosted delivery has not been requested. No live deployment changed. Recommended
next milestone: verify the corrected hosted run, then prepare the first
owner-approved delivery.
