# Local operational health

Current local source: the standalone monitor checks availability/configuration,
Airflow service health, paused DAGs, the last completed run state and stuck active
runs. Data freshness checks have moved to [dbt](dbt-freshness.md); this replaces the
previous Python timestamp, FX-date and successful-run-age rules. The new dbt gate
and event-driven callbacks were deployed and verified on 2026-11-04.

```powershell
.\.venv\Scripts\python.exe -m scripts.monitor
```

The command prints JSON and saves a unique ignored artifacts/monitoring report.
Use --no-save to print only. Exit 0 means healthy, 1 warning and 2 critical or an
unavailable/invalid probe. A warehouse lock held by a writer produces a warning;
it is never interpreted as healthy. These health findings do not send emails.

Configuration reads process environment, then working-directory .env, then defaults:

| Variable | Default |
| --- | --- |
| RETAILFLOW_MONITOR_ORDERS_URL | http://127.0.0.1:18000 |
| RETAILFLOW_MONITOR_AIRFLOW_URL | http://127.0.0.1:8080 |
| RETAILFLOW_MONITOR_AIRFLOW_USERNAME | admin |
| RETAILFLOW_MONITOR_RUN_MAX_AGE_SECONDS | 1800 |

Authentication reuses RETAILFLOW_AIRFLOW_PASSWORD; dataset version, seed and interval
are compared with the API configuration, not used as Python freshness thresholds.
Old MONITOR_ORDERS_MAX_AGE_SECONDS and MONITOR_FX_MAX_AGE_SECONDS settings are retired.
The existing user is a local admin; no new read-only RBAC role is claimed.

HTTP is restricted to loopback origins; redirects, embedded credentials and proxy
inheritance are rejected. Only exception types are reported, never raw response
bodies, environment variables or login values. The existing Docker warehouse probe
runs SELECT 1 with read-only SQL and external access disabled under the common
0660 FileLock. It never initializes a database or reads order/FX timestamps.

Reports are owner-operated internal operational metadata for local troubleshooting,
not an SLA, compliance record or external notification service. They remain ignored
by Git/delivery, with manual retention/deletion and no automatic upload. Trusted
local users and Docker remain part of the security boundary.

Failure emails now originate only from failed Airflow DAG runs; see
[the local inbox](local-email.md). There is no periodic email/check schedule. If the
scheduler is stopped, it cannot execute dbt checks or invoke a failure callback;
independent uptime alerting is not implemented by this milestone.
