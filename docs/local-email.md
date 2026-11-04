# Local alert inbox

The [dbt freshness/failure-event milestone](dbt-freshness.md) was deployed on
2026-11-04 and supersedes the former polling/reminder/recovery sender from the first
inbox deployment. Its Airflow image/route update is verified. Only terminal DAG
failure events will send automatic mail; no periodic email schedule is planned.

Status: deployed locally on 2026-11-03 after owner approval of the saved plan.
The managed inbox is healthy; a synthetic test message was captured and its body
verified through the inbox API. The normal monitoring command returned OK/no alerts.
No scheduled email task has been started or message sent to an external provider.

Mailpit captures SMTP messages and shows them in a browser. Its standard ports
are documented by the [Mailpit project](https://mailpit.axllent.org/docs/install/docker/).
The reviewed plan has been applied. Open:

- Inbox: http://127.0.0.1:8025
- SMTP: 127.0.0.1:1025
- Synthetic recipient: operator@retailflow.test
- Synthetic sender: monitor@retailflow.test

These addresses are labels in the local catcher. They require no registration,
password, Gmail account, DNS or real mailbox. They never receive internet mail.
The previous Gmail proposal is superseded; do not add app passwords to .env.

## Terraform ownership and isolation

`infra/local/mailpit.tf` adds an image, container, dedicated bridge and protected
named volume. The image uses Mailpit v1.33.0 pinned to the registry manifest digest,
with a minimal build context that prepares its data directory for UID/GID 10001.
The container runs non-root, with a read-only root filesystem, all capabilities
dropped, no privilege escalation and CPU/memory limits. Messages live only in
the persistent /data volume; the protected warehouse and source volumes are separate.

Both ports publish on 127.0.0.1 only. The bridge is separate from the platform
network, but is not an egress firewall. Docker's internal bridge did not publish
host ports on the tested Docker Desktop setup, so it was not used. Relay and
forwarding remain unconfigured, version checks and SMTP reverse DNS are disabled,
and SMTP accepts only the fixed synthetic recipient. Allowed browser Host values
are restricted to loopback names. The UI has no login: the local operator and
workstation remain trusted. This is not an internet-facing mail service or RBAC.

The image sets MP_MAX_MESSAGES=0 to disable Mailpit's automatic deletion, as
documented in its [runtime options](https://mailpit.axllent.org/docs/configuration/runtime-options/).
Messages survive container restart and image replacement using the same volume.
They can accumulate until explicitly deleted by the owner in the inbox UI. No
mailbox backup/export automation is implemented. Local volume protection is not
off-host backup or a durability guarantee.

## Use after deployment

```powershell
# Synthetic inbox connectivity test; no pipeline failure is simulated.
.\.venv\Scripts\python.exe -m scripts.monitor_mail --test-email

# Run read-only monitoring and capture any new alerts locally.
# Automatic delivery is the DAG-level failure callback after deployment.

# Check/report only, without sending or advancing notification state.
.\.venv\Scripts\python.exe -m scripts.monitor_mail --dry-run
```

The sender now requires --test-email or --dry-run for explicit diagnostics. With
neither option it refuses to run, preventing an old polling invocation from sending
mail. Automatic delivery is registered only at DAG level for terminal failed runs.
Success, running/retry states, reminders and recovery do not send messages. The
test-email option does not modify failure receipts.

Failure messages contain the allowlisted DAG name, a hashed event identity and time. Full probe
observations, credentials, tokens, dataset records, SQL, logs and exception text
are excluded. Diagnostic SMTP uses loopback; the DAG processor uses the approved
Docker-local retailflow-mailpit service and alone joins that inbox network. Both
routes hardcode the synthetic addresses;
there is no external SMTP host, login or recipient configuration. It never calls
an external email service. The UI's link-check features/browser links are not a
network isolation guarantee; use this inbox for synthetic operational messages.

Reports and delivery events are retained under ignored artifacts/monitoring.
Old mail-state.json is retained but unused. Per-failed-run receipts are now stored
under the protected Airflow logs volume, after SMTP acceptance, with a common event
lock and stable Message-ID. Mailpit duplicate-ID suppression adds protection for
callback replay. This is not exactly-once delivery or a durable outbox: callback
SMTP failure is visible in processor logs and may need explicit replay. No raw
exception text is logged. Explicit diagnostics keep monitor exit codes and use 3
for unavailable delivery; they never schedule emails or reset failed runs.

## Data ownership and verification

The repository owner owns this synthetic internal operational dataset. Lineage is
Airflow terminal DAG-failure event -> filtered email -> Mailpit SQLite database.
Approved use is local development/troubleshooting. Retention is manual; deletion
is an explicit owner UI action. Do not store real customer data or credentials in
messages. Nothing from the inbox volume or notification state enters Git/delivery.

Focused tests exercised loopback-only SMTP, filtered content, header rejection,
deduplication, reminders, severity changes, recovery and retry state. A disposable
real Mailpit container verified SMTP-to-UI capture, recipient/Host restrictions,
non-root operation, loopback bindings and message persistence after restart.
Only UUID-named disposable test resources were removed afterward. No live data
was corrupted, no full-suite rerun was used, and no image vulnerability scan is claimed.

The approved saved plan added exactly four Mailpit resources, with no existing
resource update/replacement/deletion. All 874 original backed-up blobs matched
after apply. Existing DuckDB, Azurite, PostgreSQL, scheduler and orders API container
IDs were unchanged. Both published ports were verified as loopback-only, and the
UI rejected an untrusted Host header. Deployment evidence is retained in ignored
`artifacts/local-inbox-review-20261103/deployment-checks.json`.
The final post-apply plan reports no changes and its fresh blob backup verified
876 objects. Terraform explicitly matches the image's inherited one-second health
start interval, avoiding repeated drift. No additional apply was needed for that
declaration. The inbox and ordinary monitor-to-inbox invocation are both healthy.
The dbt gate and terminal-failure callbacks are deployed. Both controlled pipeline
runs passed all phases without generating email; the existing inbox message remained
present. Failure delivery was tested with isolated callback invocation and disposable
Mailpit, rather than manufacturing a failed live run. The earlier ten-minute host
email-schedule proposal is cancelled.
