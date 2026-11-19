# Parallel batch, streaming and webhook ingestion

Status: implemented and tested locally with disposable data; no Terraform apply
or new schedule activation has occurred. The batch code, source contracts and
historical namespaces remain intact. All infrastructure is local and opt-in.

## Three sources, not three copies of orders

| Mode | Source | Durable landing | Raw tables |
| --- | --- | --- | --- |
| Existing batch | Generated orders/customers; real Frankfurter FX | Existing CSV/JSON contracts | Existing raw/privacy tables |
| Kafka stream | Generated `inventory-v1` movements | `stream-inventory/<topic>/partition=N/offset=N/` | `streaming.inventory_movements` |
| Business webhook | Generated `shipments-v1` transitions | `webhook-shipments/event=<event_id>/` | `event_driven.shipment_updates` |

Inventory events contain product, warehouse, signed quantity delta and movement
reason. Shipments contain only generated order/shipment references and status.
They reuse the batch product catalogue and orders-v3 time/seed profile for valid
references; they do not duplicate orders or revenue. New namespaces and source
checkpoints are independent of batch extraction.

The source simulators emit directly when their synthetic time slot becomes due;
they do not poll the orders API. Normal generation remains ten minutes. Five
seconds is an explicit demonstration setting. Initial startup starts at the
current slot; existing checkpoints resume interrupted work without creating a
full historical backfill. Changing a source profile requires a reviewed state
namespace change. All timestamps, event IDs and payloads are deterministic.

## Delivery and recovery

Each producer fsyncs its frozen pending event before delivery. Its source cursor
advances only after a Kafka acknowledgement or successful signed webhook response.
Lost acknowledgements retry the same event. Kafka producer idempotence reduces
broker retries, but restarts may still deliver duplicates: the end-to-end contract
is **at least once**, with warehouse event IDs and payload hashes preventing
duplicate rows and rejecting conflicting content. No exactly-once claim is made.

The inventory consumer disables automatic offset commit/storage. For each record
it validates the contract, lands canonical JSON, verifies downloaded bytes,
publishes/verifies checksum metadata last, and commits the warehouse transaction.
Only then does it synchronously commit offset+1. One-event landing units keep
partition/offset identity stable across failures and rebalances. Invalid messages
stop the consumer without skipping an offset; raw invalid bodies are not copied
to logs or an unvalidated dead-letter archive. Restart replays uncommitted data.

Shipment transitions use an HMAC-SHA256 signed body and delivery timestamp, with
a five-minute acceptance window and 16 KiB body limit. The endpoint returns 202
only after verified blob landing and warehouse commit. Busy/unavailable storage
returns 503; the simulator retries its frozen event with a new delivery signature.
Repeated event IDs return the existing result. Unknown fields and invalid status/
order references are rejected with sanitized errors, without persisting the body.

Both transports use the same warehouse FileLock (0660), protected group 10001 and
directory mode 2770. Event records, canonical hashes and per-manifest membership/
load ledger entries commit together. Existing batch writers use the same lock.
Kafka and webhook workers never acquire separate DuckDB files or bypass it.

## Infrastructure and keys

`infra/local/parallel.tf` uses Terraform Community CLI and Docker. Enablement
defaults to false (`RETAILFLOW_PARALLEL_INGESTION_ENABLED`). It prepares:

- One digest-pinned Apache Kafka 4.2.0 amd64 broker/controller in KRaft mode.
- A three-partition `retailflow.inventory.v1` topic and one-shot topic initializer.
- Separate inventory producer, inventory consumer, shipment simulator and webhook containers.
- Isolated stream/webhook networks and protected Kafka/source-state volumes.
- Loopback-only Kafka 19092 and webhook 18090; controller ports stay internal.

Workers are read-only, non-root UID/GID 10001 with dropped capabilities. The Kafka
image runs as UID 1000 and needs writable startup configuration paths; this is a
documented exception to read-only worker filesystems. Only the consumer/webhook
receive blob credentials and mount the shared warehouse. Only shipment sender/
receiver mount the signing key read-only. No Docker socket enters a worker.

Prepare the local key without displaying it:

```powershell
.venv\Scripts\python.exe -m retailflow.shipment_webhook
```

This exclusively creates ignored `.local-secrets/shipment-webhook.key`. Keep host
ACLs restricted, verify UID 10001 can read the bind mount, and retain the key
securely. The key never enters Terraform values/state, images, event payloads or
dbt's child environment. Rotations require a coordinated sender/receiver restart.
The existing Azurite emulator credential still enters local Terraform state under
the platform's established sensitive-variable workflow.

The infrastructure helper reads public enablement/interval settings from process
environment, `.env`, then defaults. Worker settings use the same priority. Edits
require container restart/reviewed deployment to take effect. Broker/webhook
outbound destinations are restricted to explicit loopback or Docker-local names;
HTTP proxies and redirects are disabled.

## dbt analytics

`parallel-pipeline` uses the existing isolated wrapper and common lock for source
freshness, clock tests and a parallel-only build. The initially paused
`retailflow_parallel_analytics` DAG runs this every ten minutes after deployment
verification. It does not schedule ingestion: Kafka consumption and webhook
requests trigger that independently. Original batch schedules remain unchanged.

Four opt-in views expose inventory movements, shipment updates, net movements
by product/warehouse and latest shipment status. Net movements are not on-hand
stock because no opening balances are supplied. Default batch builds keep these
resources disabled. No new event source is unioned into order revenue.

dbt checks inventory occurrence and ingestion freshness (30 minutes, current seed/
interval), future timestamps, uniqueness, source field rules and manifest lineage.
Shipments are business events: silence is not inherently stale, so there is no
mandatory shipment-arrival freshness threshold. Their clocks and quality are
still checked. Deliberate corruption belongs only in disposable test storage.

## Threat model, governance and limits

The repository owner owns both datasets. Classification: synthetic operational
data, with linkable synthetic order IDs in shipments. Approved uses are local
engineering, demonstrations and analytics tests; real-person data is prohibited.
Canonical payload hashes, event IDs, Kafka positions and load manifests record
lineage. Contracts forbid customer/contact fields. These controls do not establish
anonymization, legal compliance or production access control.

Kafka uses plaintext on isolated Docker/loopback listeners. Any host administrator
or actor with network access can publish; broker RBAC, TLS and high availability
are not implemented. The controlled producer emits no personal fields, and the
consumer refuses out-of-contract content, but this is not broker-side PII prevention.
The webhook signature protects authenticity/integrity, not transport encryption.
Host administrators retain access to bind-mounted keys and protected volumes.

Kafka log retention is explicitly seven days. Landed blobs, warehouse history,
source checkpoints and backups have manual retention; nothing auto-deletes them.
A consumer offline beyond Kafka retention may have a gap. Recreating/resetting
a topic behind existing partition/offset manifests fails closed; recovery needs
a separately reviewed topic/version and replay plan, not overwritten history.

Kafka is capped at 1 GiB and each worker at 256 MiB (about 2 GiB total additional
limits, plus a temporary initializer). Review host capacity and port availability
before deployment alongside the existing Airflow services. No mandatory cloud
service, subscription, trial or chargeable resource is introduced.

## Validation and deployment boundary

Unit tests cover signed requests, contract rejection, frozen retries, checksum
failures, offset acknowledgement order, duplicate/conflicting events and batch
table preservation. Opt-in container tests use disposable volumes/networks for
real Kafka resume/duplicate delivery and isolated dbt builds/corruption checks.
Never point these tests at managed volumes.

Before deployment, run CI, create/review a fresh saved Terraform plan and blob
backup, check capacity/key permissions, drain affected DAGs, and preserve a locked
warehouse snapshot. Applying that plan can rebuild existing shared images and
must be approved separately. Verify all three data paths and source checksums
before enabling the new analytics schedule. Existing older saved plans are stale
after these source changes; retain them as historical evidence, not apply inputs.

References: [Apache Kafka Docker](https://kafka.apache.org/42/getting-started/docker/),
[Python producer/consumer client](https://docs.confluent.io/kafka-clients/python/current/overview.html).

On 2026-11-19, the real Kafka test passed duplicate delivery, consumer restart/
committed-offset resume, signed webhook replay and preservation of a separate
batch table. The isolated dbt test built four views, passed its quality/freshness
checks and rejected deliberately corrupted stock movement data. A separate parse
confirmed the default batch selection still has three models and 37 tests. No
test attached a worker to the managed warehouse or deployed this extension.

After the successful runs, Docker Desktop's backend began returning internal API
errors. The live deployment preview could not be completed, and cleanup could
not be rechecked against the backend. No reset or changing apply was attempted.
The test harness now reports cleanup failures explicitly, attempts every owned
resource, and restricts deletion to its generated test namespace. A mocked
backend-failure regression passed; rerun the real test after Docker recovery.

The owner-approved single Docker restart recovered the engine on 2026-11-19;
follow-up inspection found no temporary Kafka/fixture containers. A read-only
infrastructure preview was saved under ignored
`artifacts/parallel-ingestion-preview-20261119-recovered`: 13 creates, 14 image/
container replacements and one metadata-container update, with every existing
volume preserved. The shipment key is absent from the plan. This is not an
approved release/apply plan: source publication/CI and fresh blob/locked warehouse
backups are still required. No infrastructure was applied or reset.
