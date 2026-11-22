# Agentic AI extension

Branch: `feature/agentic-ai`. The required CI gate completed successfully for
`05f588dceb8319eff9170bdea3aaa3442e5b0f55` in run 45015922921; fetching main
confirmed the same HEAD before branch creation. Existing local containers were
observed running during assessment. No AI runtime is deployed and active schedules
are not changed by this work.

## Assessment and architecture decision

The dashboard already consumes two independently verified, immutable reporting
formats: sales at the reporting root and catalog/inventory under `catalog/`.
Their readers enforce checksums, file bounds and schema/row contracts. They are
the approved AI data boundary; the live warehouse, source credentials, raw customer
data, Terraform state, Docker socket and shell are outside the agent's capability.
Monitoring is an on-demand health report rather than a freshness implementation;
dbt remains the source of pipeline freshness checks.

A small explicit Pydantic tool-calling loop is preferable to a framework here:
few capabilities, inspectable authorization, bounded calls and no new ingestion/
Airflow/dbt dependencies. The agent has its own package/runtime. Streamlit will
call an opt-in private service; disabled AI must not affect existing pages.
Mock-provider tests will be mandatory/offline; a local Ollama adapter and optional
hosted adapter are separate provider choices, without mandatory keys/subscriptions.

## Phase A: deterministic data boundary

`retailflow_ai` reuses the actual reporting readers and freezes each source's
version on first use during a turn. All calculations use integer EUR minor units;
ratios use integer HALF_UP basis points. No user/model SQL is accepted. Strict
tool schemas reject extra fields, invalid dates, unknown profiles and unknown
filter identifiers. Different sales profiles and inventory seed/interval profiles
never combine. Dates are limited to an ordered period of at most 366 days.

Tools: snapshot metadata/provenance, sales summaries, country/product/category
performance, preceding-period comparisons, FX coverage, lifetime inventory
movements and product lookup. Every result carries scope, snapshot UUID/checksum,
publication/source times, warnings and limitations. Category tools require the
catalog source. Unknown/empty data is not a fabricated zero revenue. Revenue
comparisons abstain when either period lacks complete usable FX or observed rows.

Inventories provide net movement, not on-hand stock or dated anomaly/causality
evidence. Rankings describe magnitudes rather than statistical anomaly claims.
Catalog attributes are latest observed values, including retirement, not values
valid at order time. Publication time is not source-freshness proof. Free-text
product names are excluded from model-visible lookup results to reduce data-borne
instruction injection; approved IDs/enums remain available as evidence.

## Remaining implementation phases

B: bounded provider/tool loop, local/mock integration, refusal and evidence rules.
C: optional conversational Streamlit interface and private Terraform runtime.
D: offline evaluations, security/compatibility checks and CI integration.
E: read-only operations investigation only after the analyst foundation passes;
otherwise document a separate follow-up PR. No remediation capability is planned.

The final runbook will record commands, actual evaluations, costs, limitations and
an interview demonstration. No reliability percentage is claimed before measurement.
