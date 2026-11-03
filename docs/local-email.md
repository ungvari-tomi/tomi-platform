# Local alert inbox

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
.\.venv\Scripts\python.exe -m scripts.monitor_mail

# Check/report only, without sending or advancing notification state.
.\.venv\Scripts\python.exe -m scripts.monitor_mail --dry-run
```

The normal command invokes the existing monitor and sends a message when alerts
first appear or their component/code/severity changes. Unchanged alerts are
suppressed for six hours, then reminded. Recovery is emailed only after a previously
emailed incident clears; an initially healthy platform produces no email. The
test-email option does not modify incident state.

Messages contain codes, component names, severity and check time only. Full probe
observations, credentials, tokens, dataset records, SQL, logs and exception text
are excluded. The sender hardcodes loopback SMTP and the synthetic addresses;
there is no external SMTP host, login or recipient configuration. It never calls
an external email service. The UI's link-check features/browser links are not a
network isolation guarantee; use this inbox for synthetic operational messages.

Reports and delivery events are retained under ignored artifacts/monitoring.
The locked mail-state.json contains only alert fingerprints and send timestamps.
Delivery state is atomically advanced after SMTP acceptance, so a failed send
can retry on the next invocation. SMTP acceptance is not proof that a person read
the inbox; a crash after acceptance but before state publication can cause a
duplicate. Corrupt/unrecognized state fails visibly rather than silently resetting.
Exit codes 0/1/2 preserve monitor outcomes; 3 means monitoring/delivery could not
complete. Events report exception types only. No retry loop or automatic schedule
is introduced by running this command.

## Data ownership and verification

The repository owner owns this synthetic internal operational dataset. Lineage is
the existing read-only monitor -> filtered email -> Mailpit SQLite database.
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
Next milestone: a reviewed Terraform-managed ten-minute host schedule independent
of Airflow.
