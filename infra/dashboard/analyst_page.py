"""Optional analyst UI and fixed private endpoint client; no model credentials."""

import json
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def request(path: str, body: dict | None = None) -> dict:
    if path not in {"/metadata", "/chat", "/health"}:
        raise ValueError("Unapproved analyst operation")
    payload = json.dumps(body).encode() if body is not None else None
    client = build_opener(ProxyHandler({}), NoRedirect())
    req = Request(
        "http://retailflow-analyst:8090" + path,  # fixed Docker-local origin
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with client.open(req, timeout=50) as response:  # nosec B310
            data = response.read(256_001)
            if len(data) > 256_000:
                raise ValueError()
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (HTTPError, URLError, TimeoutError, ValueError, OSError):
        raise ValueError(
            "The analyst service is unavailable. Existing report pages still work."
        ) from None


def render():
    import streamlit as st

    st.subheader("AI business analyst")
    st.caption(
        "Read-only analysis of verified synthetic reporting snapshots. No live warehouse access."
    )
    try:
        health, metadata = request("/health"), request("/metadata")
        st.caption(f"Provider: {health['provider']}")
        if health["provider"] == "mock":
            st.info("Offline demo mode: deterministic mock planner, not a language model.")
        sources = [s for s in ("sales", "catalog") if metadata[s]["available"]]
        if not sources:
            st.info("No verified reporting snapshots are available yet.")
            return
        source = st.selectbox("Analysis snapshot source", sources)
        profiles = metadata[source]["sales_profiles"]
        context = {"source": source}
        if profiles:
            dataset = st.selectbox("Analysis sales dataset", [p["dataset_id"] for p in profiles])
            profile = next(p for p in profiles if p["dataset_id"] == dataset)
            dates = st.date_input(
                "Analysis dates (UTC)",
                (
                    date.fromisoformat(profile["first_date"]),
                    date.fromisoformat(profile["last_date"]),
                ),
            )
            if len(dates) != 2:
                st.info("Select both dates before asking a sales question.")
                return
            context.update(
                dataset_id=dataset, start_date=dates[0].isoformat(), end_date=dates[1].isoformat()
            )
        inventory = metadata.get("catalog", {}).get("inventory_profiles", [])
        if inventory:
            selected = st.selectbox(
                "Analysis inventory profile",
                inventory,
                format_func=lambda p: f"seed {p['seed']}, interval {p['interval_seconds']} seconds",
            )
            context.update(
                inventory_seed=selected["seed"],
                inventory_interval_seconds=selected["interval_seconds"],
            )
        signature = json.dumps(context, sort_keys=True)
        if st.session_state.get("analyst_scope") != signature:
            st.session_state.analyst_scope = signature
            st.session_state.analyst_messages = []
        if st.button("Clear conversation"):
            st.session_state.analyst_messages = []
        messages = st.session_state.analyst_messages
        for message in messages:
            with st.chat_message(message["role"]):
                st.text(message["content"])
                if "answer" in message:
                    evidence_view(st, message["answer"])
        prompt = st.chat_input(
            "Ask about sales, FX coverage, products or inventory movements", max_chars=4000
        )
        if prompt:
            history = [{"role": m["role"], "content": m["content"][:4000]} for m in messages[-6:]]
            with st.spinner("Checking verified evidence..."):
                answer = request(
                    "/chat", {"question": prompt, "context": context, "history": history}
                )
            messages.extend(
                [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": answer["text"], "answer": answer},
                ]
            )
            st.session_state.analyst_messages = messages[-6:]
            st.rerun()
    except (ValueError, KeyError, TypeError):
        st.warning(
            "The analyst service is unavailable or returned invalid metadata. Try a report page."
        )


def evidence_view(st, answer):
    st.caption(f"Status: {answer['status']} | {answer['latency_ms']} ms")
    for warning in answer.get("warnings", []):
        st.warning(warning)
    with st.expander("Evidence and tool execution"):
        st.json({"evidence": answer.get("evidence", {}), "trace": answer.get("trace", [])})
