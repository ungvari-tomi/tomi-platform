"""Offline/optional-model evaluation harness; result artifacts contain no chat text."""

import time
from dataclasses import dataclass, field

from retailflow_ai.agent import Agent, Question
from retailflow_ai.snapshots import Reports


@dataclass(frozen=True)
class Case:
    name: str
    question: Question
    expected_status: str
    expected_tools: tuple[str, ...] = ()
    # Paths address deterministic facts in e1; expectations are independently authored.
    expected_facts: dict[str, int | str | None] = field(default_factory=dict)


def evaluate(agent: Agent, root, cases: list[Case]) -> dict:
    rows = []
    for case in cases:
        started = time.monotonic()
        result = agent.run(case.question, Reports(root))
        numeric = []
        for path, expected in case.expected_facts.items():
            actual = result.evidence.get("e1")
            actual = actual.facts if actual else None
            for part in path.split("."):
                actual = actual.get(part) if isinstance(actual, dict) else None
            # A missing path must not pass an expected NULL check.
            present = "e1" in result.evidence
            if present:
                value = result.evidence["e1"].facts
                for part in path.split("."):
                    if not isinstance(value, dict) or part not in value:
                        present = False
                        break
                    value = value[part]
            numeric.append(present and actual == expected and type(actual) is type(expected))
        tools_ok = tuple(t.tool for t in result.trace) == case.expected_tools
        status_ok = result.status == case.expected_status
        cited = (
            all(f"[{key}]" in result.text for key in result.evidence)
            if result.status == "answered"
            else True
        )
        provenance = all(bool(value.evidence) for value in result.evidence.values())
        rows.append(
            {
                "case": case.name,
                "tool_selection_correct": tools_ok,
                "status_correct": status_ok,
                "fact_checks_passed": sum(numeric),
                "fact_checks": len(numeric),
                "evidence_attribution_correct": cited and provenance,
                "tool_calls": len(result.trace),
                "latency_ms": round((time.monotonic() - started) * 1000),
                "passed": tools_ok and status_ok and all(numeric) and cited and provenance,
            }
        )
    return {
        "provider": agent.provider.name,
        "scope": "Authored cases only; mock results do not measure real-model reliability.",
        "cases": rows,
        "passed": sum(row["passed"] for row in rows),
        "total": len(rows),
    }
