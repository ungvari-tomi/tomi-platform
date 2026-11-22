"""Sales dashboard over verified, aggregate-only snapshots."""

import os
from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st
from reporting import current_version, load_snapshot

st.set_page_config(page_title="RetailFlow | Sales", page_icon="📊", layout="wide")


@st.cache_data(max_entries=4, show_spinner=False)
def snapshot(root: str, version: str):
    return load_snapshot(Path(root), version)


def main() -> None:
    st.title("RetailFlow")
    page = st.sidebar.radio("View", ["Sales overview", "Catalog and inventory"])
    if page == "Catalog and inventory":
        from catalog_page import render

        render(Path(os.environ.get("RETAILFLOW_REPORTING_DIR", "/reports")))
        return
    st.caption("SALES OVERVIEW · Synthetic retail data")
    st.button("Refresh data", help="Check for the latest successfully published snapshot.")
    root = Path(os.environ.get("RETAILFLOW_REPORTING_DIR", "/reports"))
    try:
        version = current_version(root)
        manifest, rows = snapshot(str(root), version)
    except FileNotFoundError:
        if not (root / "current.json").exists():
            st.info("Waiting for the first successfully published sales report.")
        else:
            st.error(
                "The sales report could not be verified. Please check the publishing pipeline."
            )
        return
    except Exception:
        # Never render database errors, filesystem paths or unverified records.
        st.error("The sales report could not be verified. Please check the publishing pipeline.")
        return
    st.caption(
        f"Published: {manifest['published_at']} · Orders through: "
        f"{manifest['latest_order_at'] or 'No orders'}"
    )
    if not rows:
        st.info("The report contains no orders yet.")
        return
    data = pd.DataFrame(rows)
    data["revenue_eur_minor"] = pd.Series([row["revenue_eur_minor"] for row in rows], dtype=object)
    profiles = sorted(data["dataset_id"].unique())
    profile = st.sidebar.selectbox("Dataset", profiles)
    st.sidebar.caption("Each dataset is a separate generator profile; profiles are never combined.")
    data = data[data["dataset_id"] == profile]
    countries = st.sidebar.multiselect("Countries", sorted(data["country_code"].unique()))
    products = st.sidebar.multiselect("Products", sorted(data["product_id"].unique()))
    currencies = st.sidebar.multiselect(
        "Original currencies", sorted(data["currency_code"].unique())
    )
    earliest, latest = data["order_date"].min(), data["order_date"].max()
    dates = st.sidebar.date_input(
        "Order dates (UTC)", (earliest, latest), min_value=earliest, max_value=latest
    )
    if len(dates) != 2:
        st.info("Choose both a start date and an end date.")
        return
    for column, selected in [
        ("country_code", countries),
        ("product_id", products),
        ("currency_code", currencies),
    ]:
        if selected:
            data = data[data[column].isin(selected)]
    data = data[(data["order_date"] >= dates[0]) & (data["order_date"] <= dates[1])]
    if data.empty:
        st.info("No orders match these filters.")
        return
    orders = int(data["order_count"].sum())
    covered = int(data["fx_covered_orders"].sum())
    missing = int(data["fx_uncovered_orders"].sum())
    # Python integers/Decimal keep monetary totals exact; chart coordinates are visual only.
    cents = sum(int(value) for value in data["revenue_eur_minor"] if value is not None)
    columns = st.columns(4)
    columns[0].metric(
        "EUR revenue · covered orders",
        f"€{cents // 100:,}.{cents % 100:02d}" if covered else "Unavailable",
    )
    columns[1].metric("Orders", f"{orders:,}")
    columns[2].metric("Units sold", f"{int(data['units_sold'].sum()):,}")
    columns[3].metric("EUR valuation coverage", f"{covered / orders:.1%}")
    if missing:
        st.warning(
            f"{missing:,} orders lack usable exchange rates and are excluded from EUR revenue. "
            "Order and unit counts include them."
        )
    st.subheader("Sales over time")
    daily = data.groupby("order_date", as_index=True).agg(
        orders=("order_count", "sum"),
        covered_orders=("fx_covered_orders", "sum"),
        revenue_eur_minor=(
            "revenue_eur_minor",
            lambda values: sum(int(v) for v in values if v is not None),
        ),
    )
    daily["Revenue (EUR)"] = [
        float(Decimal(int(row.revenue_eur_minor)) / 100) if row.covered_orders else None
        for row in daily.itertuples()
    ]
    revenue, volume = st.tabs(["EUR revenue", "Order volume"])
    with revenue:
        st.line_chart(daily[["Revenue (EUR)"]])
    with volume:
        st.bar_chart(daily[["orders"]])
    st.subheader("Product performance")
    products_summary = data.groupby("product_id")[
        ["order_count", "units_sold", "fx_covered_orders", "fx_uncovered_orders"]
    ].sum()
    st.dataframe(products_summary.sort_values("order_count", ascending=False), width="stretch")
    st.caption(
        "Revenue uses dbt's exact per-order EUR rounding. "
        "Publication time describes this snapshot. Source freshness is checked by dbt."
    )


if __name__ == "__main__":
    main()
