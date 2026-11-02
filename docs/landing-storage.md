# Azurite landing storage

Azurite is managed by local Terraform. Migration preserved existing blobs and
finished with no remaining plan changes. See [the Terraform runbook](terraform.md).

The current flow is orders API → CSV and Frankfurter → JSON → Azurite. DuckDB
loading is intentionally deferred. The Azure Blob SDK separates storage access
from serialization and source calls; no additional paid service is required.

## Windows setup and operation

Run commands from the repository root with Docker Desktop's Linux engine running.
Install the locked environment as described in [environment.md](environment.md).

```powershell
.\.venv\Scripts\python.exe -m retailflow.blob_storage init
.\.venv\Scripts\python.exe -m retailflow.infrastructure init
.\.venv\Scripts\python.exe -m retailflow.infrastructure plan
# Review the summary before applying:
.\.venv\Scripts\python.exe -m retailflow.infrastructure apply
.\.venv\Scripts\python.exe -m retailflow.blob_storage check
```

Initialization appends a random emulator-only key to the ignored `.env` when none
is configured; existing API configuration is preserved. Terraform and Python read
the same `AZURITE_ACCOUNT_KEY`; a process environment override wins. Never print
the key, raw Terraform state, or saved plan JSON in shared logs. Validate without
disclosing it using `python -m retailflow.infrastructure validate`.

Start the orders API separately using the README command. Then:

```powershell
.\.venv\Scripts\python.exe -m retailflow.landing orders --limit 5
.\.venv\Scripts\python.exe -m retailflow.landing exchange-rates --date 2026-10-31
.\.venv\Scripts\python.exe -m retailflow.blob_storage list
```

Each landing command prints a JSON `manifest` path. Copy that into a download call:

```powershell
.\.venv\Scripts\python.exe -m retailflow.landing download --manifest '<manifest path>' --output artifacts/sample.csv
```

Use `.json` for exchange rates. The file is created only after checksum verification;
existing output files are not replaced. `artifacts/` is ignored by Git. Use
`docker stop tomi-platform-azurite-1` to stop the emulator and
`docker start tomi-platform-azurite-1` to restart. The named
`tomi-platform_azurite-data` volume retains blobs across restarts; do not
remove that volume unless intentionally deleting all local landed data.

If port 10000 is occupied or the Docker engine is stopped, fix that before running
checks. If authentication fails, verify both processes use the same local key and
generate/review a new Terraform plan after a key change, then apply it to recreate
the container with its existing volume. Do not commit old or new keys. Cloud key
rotation is not implemented.

## Format and completion contracts

The `landing` container contains paths like:

```text
orders/ingestion_date=2026-10-31/batch=<uuid>/data.csv
orders/ingestion_date=2026-10-31/batch=<uuid>/metadata.json
exchange-rates/ingestion_date=2026-10-31/batch=<uuid>/data.json
exchange-rates/ingestion_date=2026-10-31/batch=<uuid>/metadata.json
```

- Orders: UTF-8 CSV, fixed header, LF newlines, UTC timestamps, integer minor-unit
  prices/totals. This is a validated conversion, not a byte-identical raw API body.
  CSV rows retain every current order field. Unexpected columns, unsupported
  dataset versions, and invalid totals fail before upload. Metadata retains the
  page's version, seed, interval, pagination, counts, and source-response checksum.
- Exchange rates: the original HTTPX response-body bytes, without JSON
  reserialization. HTTP content compression may already have been decoded by the
  HTTP client; this is not a wire-level packet capture. The existing FX client
  validates the body first and preserves requested/publication dates separately.
- Both: separate metadata includes source URL/query parameters, UTC retrieval time,
  format, byte length, and SHA-256. No credential belongs in a source URL.
- Data uploads use `overwrite=False`. Existing identical bytes are accepted;
  different bytes at the same name are refused. After downloading and verifying
  the payload, metadata is written last. Readers must treat only batches with
  metadata as complete and verify the referenced payload checksum.

This is application-level write protection, not Azure immutable storage/WORM.
The account key can overwrite/delete data through other clients. A failure before
metadata leaves an incomplete orphan payload; automatic cleanup is not implemented.
Replaying exactly the same saved payload and metadata is safe. A new live API call
normally gets a new UUID; reusing `--batch-id` with changed content/metadata fails.
There is no cross-batch record deduplication or automatic checkpoint advancement.

Orders fetches **one page** per invocation (latest 20 by default). Use `--after-id`
and `--limit` explicitly for historical pages; read `next_after_id`/`has_more` from
the stored metadata. There is no automatic full-history fetch. Orders HTTP calls
use timeouts but no application retry loop yet; FX retains its bounded retries.
Blob requests use bounded SDK retries. Checksums detect accidental corruption,
not malicious changes by someone who can replace data and metadata together.

## Security, governance, and cost boundaries

The Terraform-managed service uses pinned Azurite 3.37.0, persistent local storage, disabled
telemetry, and only publishes the blob port on `127.0.0.1:10000`. Queue/table
services are not started. SDK calls accept only loopback or the exact approved
Azurite Docker endpoint, with an explicitly supported
storage API version; this command cannot accidentally target a paid Azure account.

Azurite requires the generated shared key. Integration tests verify that a wrong
key is denied with HTTP 403. That is authentication evidence, not Azure RBAC or
least-privilege service identities. There is no TLS, disk encryption guarantee,
managed identity, cloud audit logging, or WORM enforcement. This is local-only
emulation; do not expose its port or use real cloud credentials.

The project maintainer owns the landing pipeline. Synthetic order data is internal
demonstration data without personal information; FX is public ECB reference data
distributed by Frankfurter, subject to the linked source terms in
[exchange-rates.md](exchange-rates.md). Metadata supports source-to-blob lineage.
Retention, deletion automation, invalid-record quarantine, and warehouse lineage
are not implemented. Local disk usage grows with batches and must be managed.

There are no Azure subscriptions, trials, credits, or chargeable resource creation.
Docker images and dependencies need downloads; local hardware/electricity and
disk are your own resources. Use Docker under its applicable license; no cloud
runtime is involved. Cloud deployment and container vulnerability scanning remain
unverified/unimplemented in this step.

## Verification

Offline tests cover CSV value preservation, original FX JSON bytes, checksums,
metadata, schema-change rejection, incomplete uploads, and repeatable batch writes.
Run all tests with the live emulator enabled:

```powershell
$env:RETAILFLOW_TEST_AZURITE = '1'
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
Remove-Item Env:RETAILFLOW_TEST_AZURITE
```

Without that variable, the two live emulator tests are skipped. They leave small
verification blobs under `checks/`; they never connect to real Azure. Source and
FX unit tests use HTTPX transports and fixtures without live internet access.

On 2026-10-31, real orders and Frankfurter responses were uploaded into Azurite
and downloaded through the CLI, with checksums verified: five CSV orders (391
bytes) and original FX JSON (64 bytes). Live tests confirmed content-type metadata,
identical-write acceptance, different-write refusal, and invalid-key denial.
The final full run passed 51 tests with live Azurite enabled. Ruff lint/format,
Bandit, Compose validation, lockfile/dependency consistency, pip-audit, and the
maintained-source secret scan passed. The audit reported no known dependency
vulnerabilities; the secret scan found no findings (credential verification and
Git history scanning were not performed). No container vulnerability scan ran.
After the Terraform migration, the full suite passed 57 tests against the new
container. All seven original backup blobs matched after replacement; the
follow-up Terraform plan had no changes and a new blob round-trip passed.

References: [Microsoft Azurite](https://github.com/Azure/Azurite),
[Azure Blob Python uploads](https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-upload-python).
