"""Catalog-aware sales and net inventory movement over verified snapshots only."""

from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st
from catalog_reporting import load_snapshot
from reporting import current_version


@st.cache_data(max_entries=4, show_spinner=False)
def snapshot(root: str, version: str):
    return load_snapshot(Path(root), version)


def money(cents: int) -> str:
    return f"EUR {cents // 100:,}.{cents % 100:02d}"


def render(root: Path) -> None:
    st.caption("CATALOG AND INVENTORY - Synthetic retail data")
    st.button("Refresh catalog snapshot")
    root = root / "catalog"
    try:
        version = current_version(root)
        manifest, tables = snapshot(str(root), version)
    except FileNotFoundError:
        if not (root / "current.json").exists():
            st.info("Waiting for the first catalog reporting snapshot.")
        else:
            st.error("The catalog report could not be verified. Check the publishing pipeline.")
        return
    except Exception:
        st.error("The catalog report could not be verified. Check the publishing pipeline.")
        return
    st.caption(
        f"Snapshot published: {manifest['published_at']} | Catalog checked: "
        f"{manifest['catalog_retrieved_at']} | Inventory through: {manifest['latest_inventory_at']}"
    )
    st.caption("Captured by the six-hour catalog workflow. This is a snapshot, not a live feed.")
    products = pd.DataFrame(tables["products"])
    categories = st.sidebar.multiselect(
        "Catalog categories", sorted([*products["category"].unique(), "Unmatched"])
    )
    status = st.sidebar.multiselect("Catalog status", ["active", "retired", "Unmatched"])
    sales_tab, inventory_tab, products_tab = st.tabs(
        ["Category sales", "Inventory movements", "Product catalog"]
    )
    with sales_tab:
        sales(tables["catalog_sales"], categories, status)
    with inventory_tab:
        inventory(tables["inventory_movements"], categories, status)
    with products_tab:
        filtered = products
        if categories:
            filtered = filtered[filtered["category"].isin(categories)]
        if status:
            filtered = filtered[filtered["catalog_status"].isin(status)]
        st.caption(
            "Latest observed attributes, including retired products; not order-time validity."
        )
        st.dataframe(filtered.sort_values("product_id"), hide_index=True, width="stretch")


def filtered_data(rows: list[dict], categories: list, status: list) -> pd.DataFrame:
    data = pd.DataFrame(rows)
    if data.empty:
        return data
    for column in ("product_name", "category", "catalog_status"):
        data[column] = data[column].fillna("Unmatched")
    if categories:
        data = data[data["category"].isin(categories)]
    if status:
        data = data[data["catalog_status"].isin(status)]
    return data


def sales(rows: list[dict], categories: list, status: list) -> None:
    data = filtered_data(rows, categories, status)
    if data.empty:
        st.info("No sales match the catalog filters.")
        return
    data["revenue_eur_minor"] = pd.Series(
        [row["revenue_eur_minor"] for row in rows], index=range(len(rows)), dtype=object
    ).loc[data.index]
    profile = st.selectbox("Sales dataset", sorted(data["dataset_id"].unique()))
    data = data[data["dataset_id"] == profile]
    countries = st.multiselect("Sales countries", sorted(data["country_code"].unique()))
    earliest, latest = data["order_date"].min(), data["order_date"].max()
    dates = st.date_input(
        "Catalog sales dates (UTC)", (earliest, latest), min_value=earliest, max_value=latest
    )
    if len(dates) != 2:
        st.info("Choose both a start date and an end date.")
        return
    if countries:
        data = data[data["country_code"].isin(countries)]
    data = data[(data["order_date"] >= dates[0]) & (data["order_date"] <= dates[1])]
    if data.empty:
        st.info("No sales match these filters.")
        return
    orders = sum(int(v) for v in data["order_count"])
    covered = sum(int(v) for v in data["fx_covered_orders"])
    missing = sum(int(v) for v in data["fx_uncovered_orders"])
    cents = sum(int(v) for v in data["revenue_eur_minor"] if v is not None)
    columns = st.columns(3)
    columns[0].metric(
        "Catalog sales - covered EUR revenue", money(cents) if covered else "Unavailable"
    )
    columns[1].metric("Catalog orders", f"{orders:,}")
    columns[2].metric("Catalog EUR coverage", f"{covered / orders:.1%}")
    if missing:
        st.warning(
            f"{missing:,} orders lack usable FX and are excluded from EUR revenue, "
            "but remain in counts."
        )
    aggregate = []
    for category, group in data.groupby("category", sort=True):
        count = sum(int(v) for v in group["order_count"])
        has_fx = sum(int(v) for v in group["fx_covered_orders"])
        total = sum(int(v) for v in group["revenue_eur_minor"] if v is not None)
        aggregate.append(
            {
                "Category": category,
                "Orders": count,
                "Units": sum(int(v) for v in group["units_sold"]),
                "Covered EUR revenue": money(total) if has_fx else "Unavailable",
                "Chart EUR": float(Decimal(total) / 100) if has_fx else None,
            }
        )
    summary = pd.DataFrame(aggregate).set_index("Category")
    st.bar_chart(summary[["Orders"]])
    st.dataframe(summary.drop(columns="Chart EUR"), width="stretch")
    st.caption(
        "One sales dataset at a time. EUR amounts use exact dbt valuation of original currencies."
    )


def inventory(rows: list[dict], categories: list, status: list) -> None:
    data = filtered_data(rows, categories, status)
    if data.empty:
        st.info("No inventory movements match the catalog filters.")
        return
    profiles = sorted({(int(r["seed"]), int(r["interval_seconds"])) for r in rows})
    labels = {f"Seed {seed} / {interval}s": (seed, interval) for seed, interval in profiles}
    profile = st.selectbox("Inventory source profile", list(labels))
    seed, interval = labels[profile]
    data = data[(data["seed"] == seed) & (data["interval_seconds"] == interval)]
    warehouses = st.multiselect("Warehouses", sorted(data["warehouse_id"].unique()))
    if warehouses:
        data = data[data["warehouse_id"].isin(warehouses)]
    if data.empty:
        st.info("No inventory movements match this profile and filters.")
        return
    columns = st.columns(4)
    for column, label, field in zip(
        columns,
        ["Movement events", "Inbound units", "Outbound units", "Net units moved"],
        ["movement_count", "inbound_units", "outbound_units", "net_quantity_moved"],
        strict=True,
    ):
        column.metric(label, f"{sum(int(v) for v in data[field]):,}")
    st.caption(
        "Net movement since ingestion began. No opening balance is supplied; "
        "this is not stock on hand."
    )
    st.dataframe(
        data[
            [
                "product_id",
                "product_name",
                "category",
                "warehouse_id",
                "movement_count",
                "inbound_units",
                "outbound_units",
                "net_quantity_moved",
                "latest_movement_at",
            ]
        ],
        hide_index=True,
        width="stretch",
    )
