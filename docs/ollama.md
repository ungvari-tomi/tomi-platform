# Local Ollama analyst

This extension connects the analyst to an actual local language model. It keeps
financial calculations, validation and evidence rendering in the existing tools.
The model interprets questions and selects tools/filters; no arbitrary SQL, shell,
warehouse, credentials or operational actions become available.

The reviewed CPU model is `qwen3:1.7b`, about 1.4 GB of weights, under Apache 2.0.
The [official model card](https://ollama.com/library/qwen3:1.7b) describes its
tool-use support. The initial target machine has 16 GB RAM, an Intel i5-8365U and
integrated graphics. Docker had about 5 GB usable memory at assessment. Ollama is
capped at 3 GiB with no extra swap and two CPU cores; one model/request at a time.
The official runtime image also requires several GB of download/disk space.

## Infrastructure and trust boundary

Terraform's existing local root owns the opt-in model image, persistent protected
`retailflow-ollama-models` volume, seeder, serving container and internal model
network. No Windows installation or host model port is required. The official
Ollama 0.40.17 image is digest-pinned; the model manifest is pinned to
`8f68893c685c3ddff2aa3fffce2aa60a30bb2da65ca488b61fff134a4d1730e7`.
Ollama's stored compatibility manifest adds the original manifest as a provenance
layer; its independently derived checksum is
`0f45bae8cd2fec097cf09b400b078cf42388a1f5702f4058d2d6d641a09db46d`.
Both that representation and all content-addressed blobs are checked.

The one-shot seeder alone uses a download bridge. It binds its temporary server
to container loopback, pulls only the reviewed model, verifies the manifest and
all downloaded blob hashes, and exits. An existing manifest is verified rather
than replaced. The serving container uses read-only model storage and joins only
an internal network shared with the analyst. Neither running service has an
internet egress route or published model port. The model sees only the bounded
questions/history and approved aggregate tool results passed by the analyst.

Both seed and serving processes run as UID/GID 10001. The server has a read-only
root, dropped capabilities, `no-new-privileges`, bounded tmpfs and
`OLLAMA_NO_CLOUD=1`. That setting disables remote inference and web search;
see [Ollama's official instructions](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features).
Hosted providers and cloud-named models also remain rejected by the adapter.
No account, API key, subscription, paid service or trial is used.

## Operation

After reviewing code, tests, available resources, backups and a saved plan, use
the existing approved deployment workflow with these ignored local settings:

```dotenv
RETAILFLOW_AI_ENABLED=true
RETAILFLOW_AI_PROVIDER=ollama
RETAILFLOW_AI_MODEL=qwen3:1.7b
RETAILFLOW_OLLAMA_ENABLED=true
```

New installations default to disabled. Once model storage is created, retain
`RETAILFLOW_OLLAMA_ENABLED=true`. To roll the analyst back, change only its provider
to `mock` through another reviewed plan; do not disable/delete protected model
storage. No original ingestion pipeline or schedule needs redesign.

Questions still use the selected source, sales dataset and date range, or selected
inventory seed/interval. The application binds that scope; the model cannot change
it. Select catalog for category analysis. Try “Rank the markets by their euro
takings”, “Which items were ordered most frequently?” and a follow-up such as
“Just Hungary please.” Inspect the trace, coverage and snapshot evidence.

CPU generation can be slow. The local provider has up to 60 seconds per decision,
180 seconds per turn, six tool calls, an 8,192-token context and a 256-token
decision output cap. Thinking output is disabled. Schemas are deduplicated in a
stable concise system prompt to reduce repeated prefill; the full authoritative
tool schemas constrain generated JSON and remain validated in Python. Unknown
country codes and invented product/warehouse filter identifiers cannot be generated.
The planner receives at most three ranking rows and two recent history messages;
omitted group counts are explicit, and full facts/provenance remain in the UI.
Mock keeps its original shorter
budgets. There is no silent fallback to mock or cloud on model errors.

The server unloads idle model weights after five minutes; the next request may
include a cold load. Model files persist. Oversized contexts fail safely rather
than claiming an unsupported answer. Small-model interpretation and CPU latency
remain practical limitations; a selected tool is not proof of a sound conclusion.

## Verification

Mandatory CI remains offline and has no LLM/model-download requirement. The focused
adapter tests validate scope binding, protocol options and paid-provider refusal.
For an explicitly authorized real-model test, build validation images and run:

```powershell
docker build --provenance=false -t retailflow-ollama:validation infra/ollama
docker build --provenance=false -f infra/analyst/Dockerfile -t retailflow-analyst:validation .
$env:RETAILFLOW_TEST_LOCAL_MODEL = '1'
uv run --locked python -m pytest -q -p no:cacheprovider tests/test_ai_ollama.py
Remove-Item Env:RETAILFLOW_TEST_LOCAL_MODEL
```

This deliberately downloads the reviewed model into disposable storage, verifies
isolation and evaluates real model-selected tools/paraphrases/follow-ups against
known synthetic snapshots. It removes only its UUID-named test containers, volumes
and network. Its measured report goes to ignored
`artifacts/ai-evaluation/ollama-local.json`. No real-model reliability percentage
is implied by mock or protocol tests. Deployment and real-model results are recorded
only after those operations actually complete.

Model weights are reproducible public artifacts, separate from business-data
backups. Keep their pinned digest and seed receipt; do not auto-delete the protected
volume. Existing blob/warehouse/report backup and retention rules are unchanged.

## Local activation record

Activated on 2026-11-22 from `feature/agentic-ai` commit
`ad6f16ada7f25c589c64e4a54f6efa63d024ae0c`, after
[successful full CI](https://github.com/ungvari-tomi/tomi-platform/actions/runs/45053608401)
and owner-authorized saved-plan deployment. The review verified 258 source files,
5,918 backed-up landed blobs and a locked warehouse backup with 14 base tables.
The scoped plan created six model resources and replaced five supporting AI/UI
resources; existing ingestion resources and protected source volumes were preserved.
Both affected reporting DAGs were restored to their original enabled states.

The optional real-model pytest passed: **1 test, four semantic cases**, in 195.18
seconds. It checked market ranking, product ordering frequency, EUR coverage and
a Hungary-only follow-up, including exact counts and NULL valuation gaps. Measured
case latencies were 97.5, 23.6, 20.2 and 33.0 seconds. These are authored synthetic
cases, not a general reliability score.

Live Streamlit checks passed for a real-model question, scoped follow-up, evidence,
both existing report pages and preserved immutable snapshot checksums. The first
live answer took 81.1 seconds. Verification confirmed non-root execution, read-only
model/report mounts, internal networking, no published model/analyst ports,
`OLLAMA_NO_CLOUD=1` and the 3 GiB/two-CPU serving limit.

Docker CPU strings were subsequently normalized in `3e5eef7`, with
[successful CI](https://github.com/ungvari-tomi/tomi-platform/actions/runs/45056648363).
The refreshed Terraform plan reported no changes; no further changing apply was
needed. Disposable evaluation storage was removed; protected model storage remains.
Private evidence is retained under ignored `artifacts/ollama-model-activation/`
and `artifacts/ai-evaluation/`.

Open <http://localhost:8501>, refresh and select **AI business analyst**. The page
shows provider `ollama` and model `qwen3:1.7b`. Select the appropriate snapshot and
date range before asking. Use supported English country names or uppercase ISO
codes, and exact product/warehouse IDs when filtering. Generation schemas constrain
filters to entities mentioned in the question or user history; the model still
chooses the tools. Verify scope and evidence, especially after follow-ups.
