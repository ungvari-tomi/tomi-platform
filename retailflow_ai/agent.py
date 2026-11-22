"""Bounded tool-using analyst with deterministic, evidence-bound answer rendering."""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Literal, Protocol

from pydantic import Field, ValidationError

from retailflow_ai.contracts import AnalysisContext, StrictModel, ToolResult
from retailflow_ai.snapshots import DataUnavailable, Reports
from retailflow_ai.tools import Tools


class Decision(StrictModel):
    kind: Literal["tool", "answer", "clarify", "abstain"]
    tool: str | None = Field(default=None, max_length=40, pattern=r"^[a-z_]+$")
    arguments: dict = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list, max_length=6)
    presentation: Literal["brief", "management_summary", "comparison"] = "brief"
    reason: Literal["question", "profile", "period", "inventory_profile", "unsupported"] = (
        "question"
    )


class HistoryItem(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class Question(StrictModel):
    question: str = Field(min_length=1, max_length=4000)
    context: AnalysisContext = Field(default_factory=AnalysisContext)
    history: list[HistoryItem] = Field(default_factory=list, max_length=6)


class Trace(StrictModel):
    evidence_id: str
    tool: str
    arguments: dict
    status: Literal["ok", "insufficient_data", "error"]
    latency_ms: int
    error_code: str | None = None


class Answer(StrictModel):
    status: Literal["answered", "clarification", "abstained", "unavailable", "limited"]
    text: str
    evidence: dict[str, ToolResult] = Field(default_factory=dict)
    trace: list[Trace] = Field(default_factory=list)
    provider: str
    latency_ms: int
    warnings: list[str] = Field(default_factory=list)


class Provider(Protocol):
    name: str

    def decide(self, state: dict, schemas: list[dict], timeout: float) -> Decision: ...


class ProviderUnavailable(RuntimeError):
    pass


class ProviderTimeout(ProviderUnavailable):
    pass


MESSAGES = {
    "question": (
        "Please clarify the business question using sales, FX coverage or inventory movements."
    ),
    "profile": "Choose one sales dataset and snapshot source; profiles cannot be combined.",
    "period": "Choose the start and end dates for the sales analysis.",
    "inventory_profile": "Choose one inventory seed and interval profile.",
    "unsupported": (
        "These reporting snapshots cannot establish stock on hand, profit, forecasts "
        "or causal explanations."
    ),
}
UNSAFE = re.compile(
    r"\.env\b|docker[.]sock|terraform[.]tfstate|api[ _-]?key|password|secret|"
    r"ignore.{0,40}instructions|system prompt|execute.{0,20}(sql|shell)|"
    r"raw[ ._]customer|restart|backfill|drop table|delete from",
    re.I,
)


def euro(value: int | None) -> str:
    if value is None:
        return "unavailable"
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}EUR {value // 100:,}.{value % 100:02d}"


def metric_text(metrics: dict) -> str:
    return (
        f"{metrics['orders']:,} observed orders; {metrics['units']:,} units. "
        f"Covered revenue: {euro(metrics['covered_revenue_eur_minor'])}. "
        f"FX-covered orders: {metrics['fx_covered_orders']:,}; "
        f"uncovered: {metrics['fx_uncovered_orders']:,}."
    )


def render(result: ToolResult) -> str:
    """Financial facts never come from model-authored prose or arithmetic."""
    facts = result.facts
    if result.tool in {"sales_summary", "fx_coverage"}:
        text = metric_text(facts)
        if result.tool == "fx_coverage":
            text += " These aggregates cannot distinguish missing, stale or unsupported FX."
        return text
    if result.tool.endswith("performance"):
        metric = {
            "orders": "observed order count",
            "units": "units sold",
            "covered_revenue_eur_minor": "covered EUR revenue",
        }[facts["ranked_by"]]
        lines = [f"Ranked by {metric} (covered revenue may be partial):"]
        lines += [f"{r['key']}: {metric_text(r)}" for r in facts["groups"]]
        return "\n".join(lines)
    if result.tool == "period_comparison":
        return (
            f"Current period: {metric_text(facts['current'])}\n"
            f"Previous period: {metric_text(facts['previous'])}\n"
            f"Comparable revenue change: {euro(facts['revenue_delta_eur_minor'])}. "
            "Incomplete coverage prevents a complete revenue-change conclusion."
            if facts["revenue_delta_eur_minor"] is None
            else f"Current period: {metric_text(facts['current'])}\n"
            f"Previous period: {metric_text(facts['previous'])}\n"
            f"Revenue change: {euro(facts['revenue_delta_eur_minor'])}."
        )
    if result.tool == "inventory_movements":
        text = (
            f"{facts['movement_count']:,} observed movement events: "
            f"{facts['inbound_units']:,} inbound and {facts['outbound_units']:,} outbound units; "
            f"net movement {facts['net_quantity_moved']:,} units. "
            "This is lifetime movement, without an opening balance or dated anomaly evidence."
        )
        ranks = "\n".join(
            f"{r['product_id']} / {r['warehouse_id']}: net {r['net_quantity_moved']:,} units"
            for r in facts["largest_absolute_net_movements"]
        )
        return text + (
            "\nLargest absolute movements (not proven anomalies):\n" + ranks if ranks else ""
        )
    if result.tool == "catalog_lookup":
        return (
            f"{facts['product_id']} / {facts['sku']}: {facts['category']}, "
            f"{facts['catalog_status']}, observed revision {facts['revision']}. "
            "Metadata describes the latest observation, not historical order-time validity."
        )
    return "Verified snapshot versions, profiles and date bounds are included in the evidence."


class Agent:
    def __init__(
        self,
        provider: Provider,
        *,
        max_calls: int = 6,
        total_seconds: float = 45,
        tool_seconds: float = 10,
        decision_seconds: float = 15,
    ):
        if (
            not 1 <= max_calls <= 6
            or not 0 < total_seconds <= 180
            or not 0 < tool_seconds <= 15
            or not 0 < decision_seconds <= 120
        ):
            raise ValueError("Invalid analyst budgets")
        self.provider = provider
        self.decision_seconds = decision_seconds
        self.pending = None
        self.max_calls, self.total_seconds, self.tool_seconds = (
            max_calls,
            total_seconds,
            tool_seconds,
        )

    def run(self, request: Question, reports: Reports) -> Answer:
        started = time.monotonic()
        evidence, trace = {}, []

        def answer(status, text, warnings=None):
            return Answer(
                status=status,
                text=text,
                evidence=evidence,
                trace=trace,
                provider=self.provider.name,
                latency_ms=round((time.monotonic() - started) * 1000),
                warnings=warnings or [],
            )

        if UNSAFE.search(request.question) or any(
            UNSAFE.search(h.content) for h in request.history
        ):
            return answer(
                "abstained", "I can only analyze approved reporting snapshots with read-only tools."
            )
        if re.search(
            r"stock on hand|\bprofit\b|\bmargin\b|forecast|caused|causality", request.question, re.I
        ):
            return answer("abstained", MESSAGES["unsupported"])
        tools = Tools(reports)
        state = {
            "question": request.question,
            "context": request.context.model_dump(mode="json"),
            "history": [h.model_dump() for h in request.history],
            "results": {},
        }
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analyst-read")
        try:
            for _ in range(self.max_calls + 1):
                remaining = self.total_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    return answer(
                        "limited", "Analysis time budget reached; inspect the collected evidence."
                    )
                try:
                    provider_future = pool.submit(
                        self.provider.decide,
                        state,
                        tools.schemas(),
                        min(remaining, self.decision_seconds),
                    )
                    self.pending = provider_future
                    decision = provider_future.result(timeout=min(remaining, self.decision_seconds))
                    if not isinstance(decision, Decision):
                        raise ProviderUnavailable()
                except (TimeoutError, ProviderTimeout):
                    return answer(
                        "unavailable",
                        "The local model timed out while loading or processing the question. "
                        "Please retry after it finishes; no answer was generated.",
                    )
                except Exception:
                    return answer(
                        "unavailable",
                        "The model provider is unavailable or returned an invalid response.",
                    )
                if decision.kind in {"clarify", "abstain"}:
                    return answer(
                        "clarification" if decision.kind == "clarify" else "abstained",
                        MESSAGES[decision.reason],
                    )
                if decision.kind == "answer":
                    ids = decision.evidence_ids
                    if not ids or len(set(ids)) != len(ids) or any(i not in evidence for i in ids):
                        return answer(
                            "abstained",
                            "No valid evidence was selected; no conclusion is supported.",
                        )
                    text = "\n\n".join(f"[{i}] {render(evidence[i])}" for i in ids)
                    if decision.presentation == "management_summary":
                        text = "Management summary for the selected observed period:\n\n" + text
                    warnings = sorted(
                        {w for i in ids for w in evidence[i].warnings + evidence[i].limitations}
                    )
                    status = (
                        "abstained"
                        if all(evidence[i].status == "insufficient_data" for i in ids)
                        else "answered"
                    )
                    return answer(status, text, warnings)
                if len(trace) >= self.max_calls:
                    return answer(
                        "limited", "Tool-call budget reached; inspect the collected evidence."
                    )
                name = decision.tool
                call_started = time.monotonic()
                call_id = f"e{len(trace) + 1}"
                arguments = {}
                try:
                    if name not in Tools.SCHEMAS:
                        raise DataUnavailable("unapproved_tool")
                    if name == "category_performance" and request.context.source != "catalog":
                        return answer(
                            "clarification",
                            "Choose the catalog snapshot source for category analysis.",
                        )
                    supplied = dict(decision.arguments)
                    context = request.context
                    model = Tools.SCHEMAS[name][0]
                    bound = {}
                    if "dataset_id" in model.model_fields:
                        if context.dataset_id is None:
                            return answer("clarification", MESSAGES["profile"])
                        if context.start_date is None or context.end_date is None:
                            return answer("clarification", MESSAGES["period"])
                        bound = {
                            key: getattr(context, key)
                            for key in ("source", "dataset_id", "start_date", "end_date")
                        }
                        bound = {
                            key: value.isoformat() if hasattr(value, "isoformat") else value
                            for key, value in bound.items()
                        }
                    elif name == "inventory_movements":
                        if (
                            context.inventory_seed is None
                            or context.inventory_interval_seconds is None
                        ):
                            return answer("clarification", MESSAGES["inventory_profile"])
                        bound = {
                            "seed": context.inventory_seed,
                            "interval_seconds": context.inventory_interval_seconds,
                        }
                    params = model.model_validate_json(json.dumps({**bound, **supplied}))
                    arguments = params.model_dump(mode="json")
                    context = request.context
                    if hasattr(params, "dataset_id"):
                        if (
                            context.dataset_id is None
                            or context.dataset_id != params.dataset_id
                            or context.source != params.source
                        ):
                            return answer("clarification", MESSAGES["profile"])
                        if (
                            context.start_date is None
                            or context.end_date is None
                            or (
                                params.start_date != context.start_date
                                or params.end_date != context.end_date
                            )
                        ):
                            return answer("clarification", MESSAGES["period"])
                    if name == "inventory_movements" and (
                        context.inventory_seed != params.seed
                        or context.inventory_interval_seconds != params.interval_seconds
                    ):
                        return answer("clarification", MESSAGES["inventory_profile"])
                    remaining = self.total_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        return answer("limited", "Analysis time budget reached.")
                    future = pool.submit(tools.call, name, arguments)
                    self.pending = future
                    result = future.result(timeout=min(self.tool_seconds, remaining))
                    # Validate the final result rather than trusting arbitrary tool-return objects.
                    result = ToolResult.model_validate(result.model_dump())
                    if result.tool != name:
                        raise DataUnavailable("tool_result_mismatch")
                    evidence[call_id] = result
                    state["results"][call_id] = result.model_dump(mode="json")
                    trace.append(
                        Trace(
                            evidence_id=call_id,
                            tool=name,
                            arguments=arguments,
                            status=result.status,
                            latency_ms=round((time.monotonic() - call_started) * 1000),
                        )
                    )
                except Exception as error:
                    code = (
                        "tool_timeout"
                        if isinstance(error, TimeoutError)
                        else "tool_arguments_invalid"
                        if isinstance(error, ValidationError)
                        else "tool_unavailable"
                    )
                    trace.append(
                        Trace(
                            evidence_id=call_id,
                            tool=name or "unknown",
                            arguments={},
                            status="error",
                            latency_ms=round((time.monotonic() - call_started) * 1000),
                            error_code=code,
                        )
                    )
                    return answer(
                        "unavailable",
                        "The requested evidence could not be retrieved safely.",
                        [code],
                    )
            return answer("limited", "Analysis budget reached; inspect the collected evidence.")
        finally:
            # Running read-only filesystem work cannot be force-killed in a Python thread.
            pool.shutdown(wait=False, cancel_futures=True)
