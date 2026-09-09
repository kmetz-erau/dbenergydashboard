from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy.exc import IntegrityError

from src.analytics import build_exception_table, run_analytics
from src.auth import require_password, render_logout_button
from src.exporter import build_analysis_workbook
from src.gas_analytics import build_combined_site_snapshot, run_gas_analytics
from src.gas_loader import gas_history_excel, gas_template_excel, load_gas_file
from src.loader import load_monthly_workbook, normalize_bill_history
from src.duckdb_loader import load_duckdb_bytes
from src.electric_accounts import load_electric_accounts
from src.reference_io import references_to_excel
from src.storage import (
    add_building,
    add_meter,
    add_gas_account,
    get_engine,
    init_database,
    load_buildings,
    load_demand_history,
    load_electric_history,
    load_meters,
    load_gas_accounts,
    load_gas_history,
    update_building,
    update_meter,
    update_gas_account,
    upsert_demand_history,
    upsert_electric_history,
    upsert_gas_history,
)

APP_TITLE = "ERAU Energy Dashboard"

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)


def inject_responsive_css():
    """Small CSS layer for better use of laptop/tablet widths.

    The layout itself stays responsive through Streamlit containers; this only
    adjusts padding, sidebar width, and metric typography at narrower widths.
    """
    st.markdown(
        """
        <style>
        /* Let the dashboard use the viewport instead of a narrow centered block. */
        .block-container {
            max-width: 100%;
            padding-top: 1.25rem;
            padding-bottom: 2rem;
            padding-left: clamp(0.8rem, 2.0vw, 2.25rem);
            padding-right: clamp(0.8rem, 2.0vw, 2.25rem);
        }

        /* The default sidebar can consume too much of a 1366px laptop screen. */
        [data-testid="stSidebar"] {
            min-width: 16rem !important;
            width: clamp(16rem, 22vw, 19rem) !important;
        }
        [data-testid="stSidebar"] > div:first-child {
            width: 100% !important;
        }

        /* Keep KPI values legible without forcing oversized text. */
        [data-testid="stMetric"] {
            min-width: 0;
        }
        [data-testid="stMetricLabel"] p {
            white-space: normal;
            line-height: 1.15;
            font-size: clamp(0.78rem, 0.9vw, 0.95rem);
        }
        [data-testid="stMetricValue"] {
            font-size: clamp(1.35rem, 2.0vw, 2rem);
            line-height: 1.1;
        }

        /* Slightly tighten the UI on smaller laptops/tablets. */
        @media (max-width: 1100px) {
            .block-container {
                padding-left: 0.75rem;
                padding-right: 0.75rem;
            }
            [data-testid="stSidebar"] {
                min-width: 15rem !important;
                width: 15rem !important;
            }
        }

        @media (max-width: 800px) {
            [data-testid="stMetricValue"] {
                font-size: 1.35rem;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def metric_grid(items, columns: int = 3):
    """Render KPI cards in short rows so they do not clip on laptops.

    items is an iterable of (label, value, delta) tuples.
    """
    items = list(items)
    for start in range(0, len(items), columns):
        row = items[start:start + columns]
        cols = st.columns(len(row), gap="small")
        for col, (label, value, delta) in zip(cols, row):
            col.metric(label, value, delta)


def show_plotly(fig, *, height: int | None = None):
    """Apply compact responsive defaults to Plotly charts."""
    fig.update_layout(
        autosize=True,
        margin=dict(l=12, r=12, t=52, b=18),
        height=height,
    )
    st.plotly_chart(fig, width="stretch")


def show_dataframe(df: pd.DataFrame, *, height="auto"):
    st.dataframe(df, width="stretch", height=height, hide_index=True)


def secret_or_env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


@st.cache_resource
def database_engine(database_url: str | None):
    # Cache only the connection pool/Engine. Schema checks are intentionally
    # performed on every Streamlit script run so Git hot-reloads can add tables.
    return get_engine(database_url)


@st.cache_data(show_spinner=False)
def load_uploaded_duckdb(raw: bytes, source_name: str):
    """Cache parsing of an uploaded DuckDB file by its byte content."""
    return load_duckdb_bytes(raw, source_name)


def currency(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"${value:,.0f}"


def number(value: float | None, decimals: int = 0) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value:,.{decimals}f}"


def pct(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value:+.1%}"


def admin_allowed() -> bool:
    expected = secret_or_env("ADMIN_PASSWORD")
    if not expected:
        return True
    if st.session_state.get("admin_authenticated"):
        return True
    with st.form("admin_login"):
        password = st.text_input("Admin password", type="password")
        submitted = st.form_submit_button("Unlock reference editing")
    if submitted and password == expected:
        st.session_state["admin_authenticated"] = True
        st.rerun()
    elif submitted:
        st.error("Incorrect password.")
    return False


def refresh_references():
    st.session_state.pop("reference_refresh_token", None)
    st.rerun()


def metric_card_row(latest: pd.Series | None, campus: pd.DataFrame):
    if latest is None:
        metric_grid([
            ("Month Cost", "—", None),
            ("Month kWh", "—", None),
            ("Rolling 12M Cost", "—", None),
            ("Rolling 12M kWh", "—", None),
            ("Rolling 12M Electric EUI", "—", None),
            ("Buildings Represented", "—", None),
        ])
        return

    metric_grid([
        ("Month Cost", currency(latest.get("cost")), pct(latest.get("costChangePct"))),
        ("Month kWh", number(latest.get("kWh")), pct(latest.get("kWhChangePct"))),
        ("Rolling 12M Cost", currency(latest.get("rolling12Cost")), None),
        ("Rolling 12M kWh", number(latest.get("rolling12KWh")), None),
        ("Rolling 12M Electric EUI", number(latest.get("rolling12ElectricEUI"), 1), None),
        ("Buildings Represented", str(int(latest.get("buildingCount", 0))), None),
    ])


def render_overview(bundle):
    st.title("Executive Overview")
    campus = bundle.campus_monthly
    latest = campus.iloc[-1] if not campus.empty else None
    if latest is not None:
        st.caption(f"Latest bill month in uploaded history: {latest['billMonth']:%B %Y}")
    metric_card_row(latest, campus)
    if latest is not None and len(campus) > 1:
        normal_count = campus.iloc[:-1]["buildingCount"].max()
        if normal_count and latest["buildingCount"] < 0.9 * normal_count:
            st.warning(
                f"The latest month may be incomplete: {int(latest['buildingCount'])} buildings are represented "
                f"versus a recent maximum of {int(normal_count)}."
            )

    if campus.empty:
        st.info("No mapped building data is available yet. Review FPL account mappings and meter mappings in Reference Data.")
        return

    c1, c2 = st.columns(2)
    with c1:
        fig = px.line(campus, x="billMonth", y="cost", markers=True, title="Monthly Electricity Cost")
        fig.update_yaxes(tickprefix="$", separatethousands=True)
        show_plotly(fig)
    with c2:
        fig = px.line(campus, x="billMonth", y="kWh", markers=True, title="Monthly Electricity Use")
        fig.update_yaxes(separatethousands=True)
        show_plotly(fig)

    snapshot = bundle.building_snapshot
    if not snapshot.empty:
        top = snapshot.nlargest(10, "rolling12Cost")
        fig = px.bar(
            top.sort_values("rolling12Cost"),
            x="rolling12Cost",
            y="building_id",
            orientation="h",
            hover_data=["facility_name", "rolling12KWh", "rolling12ElectricEUI"],
            title="Top 10 Buildings by Rolling 12-Month Electricity Cost",
        )
        fig.update_xaxes(tickprefix="$", separatethousands=True)
        show_plotly(fig)

        st.subheader("Latest Building Snapshot")
        columns = [
            "building_id", "facility_name", "use_type", "area_sqft", "rolling12KWh", "rolling12Cost",
            "rolling12ElectricEUI", "rolling12CostPerSF", "effectiveCostPerKWh", "peerRank",
        ]
        show_dataframe(snapshot[[c for c in columns if c in snapshot.columns]], height=360)


def render_cost_usage(bundle):
    st.title("Cost & Usage")
    monthly = bundle.building_monthly.copy()
    if monthly.empty:
        st.info("No mapped electricity data is available.")
        return

    f1, f2 = st.columns(2)
    buildings = ["All"] + sorted(monthly["building_id"].dropna().unique().tolist())
    selected = f1.selectbox("Building", buildings)
    use_types = sorted(monthly["use_type"].dropna().unique().tolist())
    selected_use_types = f2.multiselect("Use type", use_types)
    filtered = monthly if selected == "All" else monthly[monthly["building_id"].eq(selected)]
    if selected_use_types:
        filtered = filtered[filtered["use_type"].isin(selected_use_types)]

    c1, c2, c3 = st.columns(3)
    c1.metric("Electricity Cost", currency(filtered["cost"].sum()))
    c2.metric("Electricity Use", number(filtered["kWh"].sum()) + " kWh")
    c3.metric("Effective $/kWh", f"${filtered['cost'].sum()/filtered['kWh'].sum():.3f}" if filtered["kWh"].sum() else "—")

    fig = go.Figure()
    trend = filtered.groupby("billMonth", as_index=False).agg(cost=("cost", "sum"), kWh=("kWh", "sum"))
    fig.add_trace(go.Bar(x=trend["billMonth"], y=trend["cost"], name="Cost", yaxis="y"))
    fig.add_trace(go.Scatter(x=trend["billMonth"], y=trend["kWh"], name="kWh", mode="lines+markers", yaxis="y2"))
    fig.update_layout(
        title="Cost and Usage Trend",
        yaxis=dict(title="Cost ($)"),
        yaxis2=dict(title="kWh", overlaying="y", side="right"),
        legend=dict(orientation="h"),
    )
    show_plotly(fig)

    latest = monthly.sort_values(["building_id", "billMonth"]).groupby("building_id", as_index=False).tail(1)
    latest = latest.dropna(subset=["costChangePct", "kWhChangePct"])
    if not latest.empty:
        fig = px.scatter(
            latest,
            x="kWhChangePct",
            y="costChangePct",
            size="cost",
            hover_name="building_id",
            hover_data=["facility_name", "electricEUI", "effectiveCostPerKWh"],
            title="Latest Cost Change vs Usage Change",
        )
        fig.add_hline(y=0)
        fig.add_vline(x=0)
        fig.update_xaxes(tickformat=".0%", title="Usage change vs prior bill month")
        fig.update_yaxes(tickformat=".0%", title="Cost change vs prior bill month")
        show_plotly(fig)

    st.subheader("Rate Schedule Summary")
    allocated = bundle.allocated_bills.copy()
    if not allocated.empty:
        rate_summary = (
            allocated.groupby("rate_schedule", dropna=False)
            .agg(cost=("allocatedCost", "sum"), kWh=("allocatedKWh", "sum"), meters=("fullMeterID", "nunique"), buildings=("building_id", "nunique"))
            .reset_index()
        )
        rate_summary["effectiveCostPerKWh"] = rate_summary["cost"] / rate_summary["kWh"].where(rate_summary["kWh"] > 0)
        show_dataframe(rate_summary.sort_values("cost", ascending=False), height=360)


def render_eui(bundle):
    st.title("Building EUI")
    snapshot = bundle.building_snapshot.copy()
    if snapshot.empty:
        st.info("No building EUI can be calculated yet.")
        return

    category_options = ["All"] + sorted(snapshot["eui_category"].dropna().unique().tolist())
    default_cat = category_options.index("Normal Building") if "Normal Building" in category_options else 0
    cat = st.selectbox("EUI category", category_options, index=default_cat)
    shown = snapshot if cat == "All" else snapshot[snapshot["eui_category"].eq(cat)]

    eligible = shown[shown["rolling12ElectricEUI"].notna()].copy()
    if not eligible.empty:
        fig = px.bar(
            eligible.sort_values("rolling12ElectricEUI"),
            x="rolling12ElectricEUI",
            y="building_id",
            orientation="h",
            hover_data=["facility_name", "use_type", "area_sqft", "rolling12KWh", "rolling12Cost", "rolling12Complete"],
            title="Rolling 12-Month Electric EUI (kBtu/sf/year)",
        )
        show_plotly(fig)

    st.subheader("Building Detail")
    choices = sorted(snapshot["building_id"].dropna().unique().tolist())
    selected = st.selectbox("Select building", choices, key="eui_building")
    row = snapshot[snapshot["building_id"].eq(selected)].iloc[0]
    metric_grid([
        ("Area", number(row.get("area_sqft")) + " sf" if pd.notna(row.get("area_sqft")) else "—", None),
        ("Rolling 12M kWh", number(row.get("rolling12KWh")), None),
        ("Rolling 12M Cost", currency(row.get("rolling12Cost")), None),
        ("Electric EUI", number(row.get("rolling12ElectricEUI"), 1), None),
        ("Cost / sf", f"${row.get('rolling12CostPerSF'):.2f}" if pd.notna(row.get("rolling12CostPerSF")) else "—", None),
    ])
    st.caption("Electric EUI is electricity-only site energy intensity: kWh × 3.412 ÷ gross floor area. Special energy facilities are separated from normal-building peer rankings.")

    detail = bundle.building_monthly[bundle.building_monthly["building_id"].eq(selected)]
    fig = px.line(detail, x="billMonth", y="electricEUI", markers=True, title="Monthly Electric EUI")
    show_plotly(fig)



def render_demand(bundle):
    st.title("Demand Analytics")
    monthly = bundle.building_demand_monthly.copy()
    demand_history = bundle.demand_details.copy()

    if monthly.empty:
        if demand_history.empty:
            st.info(
                "No demand data is available yet. Upload an enhanced workbook containing "
                "Selected Enriched or PDF Details with parsed bill PDFs."
            )
        else:
            st.warning(
                "Demand records were found, but none can be mapped to buildings yet. "
                "Review the demand meter exceptions in Data Quality / Reference Data."
            )
        return

    latest = bundle.building_demand_snapshot.copy()
    latest_data_date = demand_history["dateBilled"].max() if not demand_history.empty else pd.NaT
    latest_allocated = (
        bundle.allocated_demand
        .sort_values(["building_id", "fullMeterID", "dateBilled"])
        .groupby(["building_id", "fullMeterID"], as_index=False)
        .tail(1)
        .copy()
        if not bundle.allocated_demand.empty else pd.DataFrame()
    )

    total_peak = latest["billedPeakDemandKW"].sum(min_count=1)
    largest_peak = latest["billedPeakDemandKW"].max()
    total_demand_charge = latest["demandChargeAmount"].sum(min_count=1)
    median_intensity = latest["demandIntensityWPerSF"].median()
    estimated_rows = int(latest_allocated["demandChargeIsEstimated"].fillna(False).sum()) if not latest_allocated.empty else 0
    demand_accounts = demand_history["accountNumber"].nunique() if not demand_history.empty else 0
    latest_bill_month = demand_history["billMonth"].max() if not demand_history.empty and "billMonth" in demand_history.columns else pd.NaT
    latest_month_rows = (
        demand_history[demand_history["billMonth"].eq(latest_bill_month)].copy()
        if pd.notna(latest_bill_month) and "billMonth" in demand_history.columns
        else pd.DataFrame()
    )
    latest_month_accounts = latest_month_rows["accountNumber"].nunique() if not latest_month_rows.empty else 0
    latest_month_meters = latest_month_rows["fullMeterID"].nunique() if not latest_month_rows.empty else 0

    if pd.notna(latest_data_date):
        st.caption(f"Latest stored demand bill date: {latest_data_date:%B %d, %Y}. Building KPIs use each building's latest available demand bill month.")
    metric_grid([
        ("Buildings w/ Demand", f"{latest['building_id'].nunique():,}", None),
        ("Latest-Month Demand Accounts", f"{latest_month_accounts:,}", None),
        ("Latest-Month Demand Meters", f"{latest_month_meters:,}", None),
        ("Sum of Billed Meter Peaks", f"{number(total_peak, 0)} kW", None),
        ("Largest Building Peak", f"{number(largest_peak, 0)} kW", None),
        ("Demand Charges", currency(total_demand_charge), None),
        ("Median Demand Intensity", f"{number(median_intensity, 2)} W/sf", None),
        ("Estimated Charge Rows", f"{estimated_rows:,}", None),
    ])
    st.info(
        "For buildings with multiple meters, peak kW is the sum of each meter's billed peak. "
        "It is not a coincident interval peak unless those meters peak at the same time. "
        "Demand-charge dollars marked estimated are calculated from the bill's $/kW rates and billed kW."
    )

    c1, c2 = st.columns(2)
    with c1:
        top_peak = latest.dropna(subset=["billedPeakDemandKW"]).nlargest(15, "billedPeakDemandKW")
        if not top_peak.empty:
            fig = px.bar(
                top_peak.sort_values("billedPeakDemandKW"),
                x="billedPeakDemandKW",
                y="building_id",
                orientation="h",
                hover_data=["facility_name", "use_type", "area_sqft", "demandIntensityWPerSF"],
                title="Top Buildings by Billed Peak Demand",
            )
            fig.update_xaxes(title="Billed peak demand (kW)")
            show_plotly(fig, height=500)
    with c2:
        top_intensity = latest.dropna(subset=["demandIntensityWPerSF"]).nlargest(15, "demandIntensityWPerSF")
        if not top_intensity.empty:
            fig = px.bar(
                top_intensity.sort_values("demandIntensityWPerSF"),
                x="demandIntensityWPerSF",
                y="building_id",
                orientation="h",
                hover_data=["facility_name", "use_type", "area_sqft", "billedPeakDemandKW"],
                title="Top Buildings by Demand Intensity",
            )
            fig.update_xaxes(title="Demand intensity (W/sf)")
            show_plotly(fig, height=500)

    st.subheader("Building Demand Detail")
    building_choices = sorted(monthly["building_id"].dropna().unique().tolist())
    selected = st.selectbox("Building", building_choices, key="demand_building")
    detail = monthly[monthly["building_id"].eq(selected)].sort_values("billMonth").copy()
    row = detail.iloc[-1]
    metric_grid([
        ("Billed Peak", f"{number(row.get('billedPeakDemandKW'), 1)} kW", pct(row.get("demandChangePct"))),
        ("Previous Peak", f"{number(row.get('previousPeakDemandKW'), 1)} kW", None),
        ("Maximum Demand", f"{number(row.get('maximumDemandKW'), 1)} kW", None),
        ("On-Peak Demand", f"{number(row.get('onPeakDemandKW'), 1)} kW", None),
        ("Demand Intensity", f"{number(row.get('demandIntensityWPerSF'), 2)} W/sf", pct(row.get("demandIntensityChangePct"))),
        ("Demand Charge", currency(row.get("demandChargeAmount")), None),
    ])

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=detail["billMonth"],
        y=detail["billedPeakDemandKW"],
        mode="lines+markers",
        name="Billed peak kW",
    ))
    if detail["onPeakDemandKW"].notna().any():
        fig.add_trace(go.Scatter(
            x=detail["billMonth"],
            y=detail["onPeakDemandKW"],
            mode="lines+markers",
            name="On-peak demand kW",
        ))
    if detail["maximumDemandKW"].notna().any():
        fig.add_trace(go.Scatter(
            x=detail["billMonth"],
            y=detail["maximumDemandKW"],
            mode="lines+markers",
            name="Maximum demand kW",
        ))
    fig.update_layout(title=f"{selected} Demand History", yaxis_title="kW")
    show_plotly(fig)

    if detail["demandChargeAmount"].notna().any():
        fig = px.bar(
            detail,
            x="billMonth",
            y="demandChargeAmount",
            title=f"{selected} Demand-Charge History",
        )
        fig.update_yaxes(tickprefix="$", separatethousands=True, title="Demand charge ($)")
        show_plotly(fig)

    st.subheader("Latest Meter-Level Demand")
    if not latest_allocated.empty:
        shown = latest_allocated.copy()
        shown["rateSchedule"] = shown["referenceRateSchedule"].fillna(shown["rateScheduleBill"])
        cols = [
            "building_id", "facility_name", "fullMeterID", "accountNumber", "dateBilled",
            "rateSchedule", "billedPeakDemandKW", "genericDemandKW", "onPeakDemandKW",
            "maximumDemandKW", "demandChargePerKW", "maximumDemandChargePerKW",
            "demandChargeAmount", "demandChargeIsEstimated",
        ]
        show_dataframe(shown[[c for c in cols if c in shown.columns]].sort_values("billedPeakDemandKW", ascending=False), height=420)

        st.subheader("Demand Cost Composition")
        cost_cols = [
            "building_id", "facility_name", "fullMeterID", "accountNumber", "dateBilled",
            "rateSchedule", "billedPeakDemandKW", "onPeakDemandKW", "maximumDemandKW",
            "demandChargePerKW", "maximumDemandChargePerKW", "demandChargeAmount",
            "demandChargeIsEstimated",
        ]
        show_dataframe(
            shown[[c for c in cost_cols if c in shown.columns]]
            .sort_values("demandChargeAmount", ascending=False),
            height=380,
        )

        missing_rate = shown[
            shown["billedPeakDemandKW"].notna()
            & shown["demandChargePerKW"].isna()
        ].copy()
        if not missing_rate.empty:
            st.warning(
                f"{len(missing_rate):,} latest demand meter rows have measured kW but no parsed demand $/kW rate. "
                "Review these bills before using demand-charge totals for budgeting."
            )
            review_cols = [
                "building_id", "facility_name", "fullMeterID", "accountNumber",
                "dateBilled", "rateSchedule", "billedPeakDemandKW",
                "demandChargePerKW", "maximumDemandChargePerKW",
            ]
            show_dataframe(missing_rate[[c for c in review_cols if c in missing_rate.columns]], height=260)

        st.subheader("Rate Schedule Demand Summary")
        rate_summary = (
            shown.groupby("rateSchedule", dropna=False)
            .agg(
                meters=("fullMeterID", "nunique"),
                buildings=("building_id", "nunique"),
                summedBilledPeaksKW=("billedPeakDemandKW", lambda x: x.sum(min_count=1)),
                avgDemandRatePerKW=("demandChargePerKW", "mean"),
                avgMaximumRatePerKW=("maximumDemandChargePerKW", "mean"),
                demandChargeAmount=("demandChargeAmount", lambda x: x.sum(min_count=1)),
            )
            .reset_index()
            .sort_values("summedBilledPeaksKW", ascending=False)
        )
        show_dataframe(rate_summary, height=320)

    if detail["previousPeakDemandKW"].notna().sum() == 0:
        st.caption(
            "Prior-demand variance will populate automatically after at least two monthly demand uploads "
            "have been stored in the dashboard database."
        )


def render_variance(bundle):
    st.title("Variance & Exceptions")
    c1, c2, c3 = st.columns(3)
    cost_threshold_pct = c1.slider("Flag cost increase (%)", 0, 100, 15, 1)
    usage_threshold_pct = c2.slider("Flag usage increase (%)", 0, 100, 15, 1)
    eui_threshold_pct = c3.slider("Flag EUI increase (%)", 0, 100, 10, 1)
    exceptions = build_exception_table(
        bundle,
        cost_threshold_pct / 100,
        usage_threshold_pct / 100,
        eui_threshold_pct / 100,
    )
    if exceptions.empty:
        st.success("No latest-period building exceptions exceeded the selected thresholds.")
    else:
        show_dataframe(exceptions, height=360)

    st.subheader("Account-Level Bill Variance")
    cols = [
        "accountNumber", "accountName", "serviceAddress", "fullMeterID", "dateBilled",
        "totalBillAmount", "previousBillAmount", "billAmountChange", "billAmountChangePct",
        "kWh", "previousKWh", "kWhChange", "kWhChangePct", "kWhPerDayChangePct", "costPerDayChangePct",
    ]
    latest_accounts = bundle.bills.sort_values(["accountNumber", "dateBilled"]).groupby("accountNumber", as_index=False).tail(1)
    show_dataframe(latest_accounts[[c for c in cols if c in latest_accounts.columns]], height=420)


def render_data_quality(bundle):
    st.title("Data Quality")
    bills = bundle.bills
    metric_grid([
        ("Bill rows", f"{len(bills):,}", None),
        ("Accounts", str(bills["accountNumber"].nunique()), None),
        ("Meters in history", str(bills["fullMeterID"].replace("", pd.NA).nunique()), None),
        ("Unmapped meters", str(bundle.unmapped_meters["fullMeterID"].nunique() if not bundle.unmapped_meters.empty else 0), None),
        ("Stored demand rows", f"{len(bundle.demand_details):,}", None),
        ("Unmapped demand meters", str(bundle.unmapped_demand_meters["fullMeterID"].nunique() if not bundle.unmapped_demand_meters.empty else 0), None),
    ], columns=3)

    if not bundle.unmapped_meters.empty:
        st.subheader("Unmapped FPL Meters")
        show_dataframe(bundle.unmapped_meters, height=320)

    if not bundle.unmapped_demand_meters.empty:
        st.subheader("Unmapped Demand Meters")
        st.caption("Demand rows on these meters are retained in history but excluded from building-demand KPIs until mapped.")
        show_dataframe(bundle.unmapped_demand_meters, height=320)

    if not bundle.meter_suggestions.empty:
        st.subheader("Suggested New Meter Mappings")
        st.caption("Suggestions are not applied automatically. Confirm them in Reference Data before they affect EUI.")
        show_dataframe(bundle.meter_suggestions, height=320)

    if not bundle.shared_meter_issues.empty:
        st.subheader("Shared Meter Allocation Issues")
        st.warning("These meter mappings are excluded from building EUI until all building allocations are populated and sum to 100%.")
        show_dataframe(bundle.shared_meter_issues, height=320)

    if not bundle.missing_area.empty:
        st.subheader("Buildings Missing Floor Area")
        show_dataframe(bundle.missing_area, height=320)

    missing_kwh = bills[bills["kWh"].isna()]
    missing_cost = bills[bills["totalBillAmount"].isna()]
    c1, c2, c3 = st.columns(3)
    c1.metric("Rows missing kWh", len(missing_kwh))
    c2.metric("Rows missing cost", len(missing_cost))
    c3.metric("Duplicate bill keys", len(bundle.duplicate_bills))


def building_form(engine, buildings):
    st.subheader("Add a Building")
    with st.form("add_building"):
        c1, c2 = st.columns(2)
        building_id = c1.text_input("Building ID", placeholder="Building 700")
        property_id = c2.text_input("Property ID")
        facility_name = st.text_input("Facility name")
        use_type = st.text_input("Use type")
        c1, c2, c3 = st.columns(3)
        campus = c1.text_input("Campus", value="ERAU - Daytona Beach, FL")
        area_sqft = c2.number_input("Gross floor area (sf)", min_value=0.0, value=0.0, step=1.0)
        category = c3.selectbox("EUI category", ["Normal Building", "Special Energy Facility", "Exterior / Non-Building Load", "No EUI"])
        notes = st.text_area("Notes")
        submit = st.form_submit_button("Add building", type="primary")
    if submit:
        if not building_id.strip():
            st.error("Building ID is required.")
        else:
            try:
                add_building(engine, {
                    "building_id": building_id,
                    "property_id": property_id or None,
                    "campus": campus,
                    "facility_name": facility_name or None,
                    "use_type": use_type or None,
                    "area_sqft": area_sqft if area_sqft > 0 else None,
                    "eui_category": category,
                    "active": True,
                    "notes": notes or None,
                })
                st.success(f"Added {building_id}.")
                st.rerun()
            except IntegrityError:
                st.error("That Building ID already exists. Use Edit Building instead.")

    if buildings.empty:
        return
    st.subheader("Edit a Building")
    selected_id = st.selectbox("Building to edit", buildings["building_id"].tolist(), key="edit_building_select")
    row = buildings[buildings["building_id"].eq(selected_id)].iloc[0]
    with st.form("edit_building"):
        facility = st.text_input("Facility name", value=str(row.get("facility_name") or ""), key="edit_facility")
        use = st.text_input("Use type", value=str(row.get("use_type") or ""), key="edit_use")
        c1, c2, c3 = st.columns(3)
        prop = c1.text_input("Property ID", value=str(row.get("property_id") or ""), key="edit_prop")
        area = c2.number_input("Gross floor area (sf)", min_value=0.0, value=float(row.get("area_sqft") or 0.0), step=1.0, key="edit_area")
        categories = ["Normal Building", "Special Energy Facility", "Exterior / Non-Building Load", "No EUI"]
        current_cat = row.get("eui_category") if row.get("eui_category") in categories else categories[0]
        category = c3.selectbox("EUI category", categories, index=categories.index(current_cat), key="edit_category")
        active = st.checkbox("Active", value=bool(row.get("active", True)), key="edit_building_active")
        notes = st.text_area("Notes", value=str(row.get("notes") or ""), key="edit_building_notes")
        save = st.form_submit_button("Save building changes")
    if save:
        update_building(engine, int(row["id"]), {
            "property_id": prop or None,
            "facility_name": facility or None,
            "use_type": use or None,
            "area_sqft": area if area > 0 else None,
            "eui_category": category,
            "active": active,
            "notes": notes or None,
        })
        st.success("Building updated.")
        st.rerun()


def meter_form(engine, buildings, meters, bundle=None):
    st.subheader("Add a Meter")
    suggestion = None
    suggestions = bundle.meter_suggestions if bundle is not None else pd.DataFrame()
    if not suggestions.empty:
        labels = ["Manual entry"] + [f"{r.meter_id} ({r.accountNumber}) → {r.suggested_building_id or 'Review'}" for r in suggestions.itertuples(index=False)]
        selected = st.selectbox("Start from detected meter suggestion", labels)
        if selected != "Manual entry":
            suggestion = suggestions.iloc[labels.index(selected) - 1]

    building_choices = buildings[buildings["active"].fillna(True)]["building_id"].tolist() if not buildings.empty else []
    if not building_choices:
        st.warning("Add a building before adding a meter.")
        return
    default_building = suggestion.get("suggested_building_id") if suggestion is not None else None
    default_index = building_choices.index(default_building) if default_building in building_choices else 0

    with st.form("add_meter"):
        c1, c2, c3 = st.columns(3)
        meter_id = c1.text_input("Meter ID", value=str(suggestion.get("meter_id")) if suggestion is not None else "")
        building_id = c2.selectbox("Building ID", building_choices, index=default_index if building_choices else 0)
        utility = c3.selectbox("Utility", ["Electricity", "Gas", "Water", "Other"])
        c1, c2, c3 = st.columns(3)
        campus = c1.text_input("Campus", value="DB")
        rate = c2.text_input("Rate schedule")
        status = c3.selectbox("Status", ["Current", "Historical", "Shared", "Unmetered", "Suggested"])
        allocation = st.number_input("Allocation fraction (1.0 = 100%; leave 0 for unresolved shared meter)", min_value=0.0, max_value=1.0, value=1.0, step=0.05)
        notes = st.text_area("Notes", value=str(suggestion.get("reason")) if suggestion is not None else "")
        submit = st.form_submit_button("Add meter", type="primary")
    if submit:
        if not meter_id.strip() or not building_id:
            st.error("Meter ID and Building ID are required.")
        else:
            add_meter(engine, {
                "campus": campus,
                "meter_id": meter_id,
                "building_id": building_id,
                "utility": utility,
                "rate_schedule": rate or None,
                "allocation_pct": None if allocation == 0 and status == "Shared" else allocation,
                "status": status,
                "active": True,
                "notes": notes or None,
            })
            st.success(f"Added meter {meter_id} to {building_id}.")
            st.rerun()

    if meters.empty:
        return
    st.subheader("Edit a Meter Mapping")
    labels = [f"{r.id}: {r.meter_id} → {r.building_id}" for r in meters.itertuples(index=False)]
    selected_label = st.selectbox("Meter mapping to edit", labels)
    row = meters.iloc[labels.index(selected_label)]
    with st.form("edit_meter"):
        c1, c2, c3 = st.columns(3)
        rate = c1.text_input("Rate schedule", value=str(row.get("rate_schedule") or ""), key="edit_meter_rate")
        statuses = ["Current", "Historical", "Shared", "Unmetered", "Suggested"]
        current_status = row.get("status") if row.get("status") in statuses else "Current"
        status = c2.selectbox("Status", statuses, index=statuses.index(current_status), key="edit_meter_status")
        allocation = c3.number_input("Allocation fraction", 0.0, 1.0, float(row.get("allocation_pct") or 0.0), 0.05, key="edit_meter_alloc")
        active = st.checkbox("Active", value=bool(row.get("active", True)), key="edit_meter_active")
        notes = st.text_area("Notes", value=str(row.get("notes") or ""), key="edit_meter_notes")
        save = st.form_submit_button("Save meter changes")
    if save:
        update_meter(engine, int(row["id"]), {
            "rate_schedule": rate or None,
            "allocation_pct": None if allocation == 0 and status == "Shared" else allocation,
            "status": status,
            "active": active,
            "notes": notes or None,
        })
        st.success("Meter mapping updated.")
        st.rerun()



def gas_account_form(engine, buildings, gas_accounts):
    st.subheader("Add a Gas Account")
    building_choices = buildings[buildings["active"].fillna(True)]["building_id"].tolist() if not buildings.empty else []
    if not building_choices:
        st.warning("Add a building before adding a gas account.")
        return

    with st.form("add_gas_account"):
        c1, c2, c3 = st.columns(3)
        provider = c1.selectbox("Provider", ["TECO", "GAS SOUTH", "OTHER"])
        utility_account = c2.text_input("Utility account number")
        building_id = c3.selectbox("Building ID", building_choices)
        c1, c2, c3 = st.columns(3)
        erau_account = c1.text_input("ERAU account")
        gas_meter_id = c2.text_input("Gas meter ID")
        rate_schedule = c3.text_input("Rate schedule")
        account_name = st.text_input("Account / service name")
        c1, c2, c3 = st.columns(3)
        usage_group = c1.text_input("Usage group", help="Use the same group for TECO delivery and Gas South commodity bills that represent the same physical therm usage.")
        cost_component = c2.selectbox("Cost component", ["Bundled / Utility", "Utility / Delivery", "Supplier / Commodity", "Other", "Unknown"])
        end_use = c3.selectbox("End use", ["Building", "Generator", "Process", "Unknown"])
        c1, c2 = st.columns(2)
        usage_authoritative = c1.checkbox("Authoritative therm source", value=True, help="If multiple provider bills share one usage group, authoritative rows are used for therms while all costs are summed.")
        include_in_site_eui = c2.checkbox("Include therms in Site EUI", value=end_use != "Generator")
        notes = st.text_area("Notes")
        submit = st.form_submit_button("Add gas account", type="primary")
    if submit:
        if not utility_account.strip():
            st.error("Utility account number is required.")
        else:
            try:
                add_gas_account(engine, {
                    "provider": provider,
                    "utility_account": utility_account,
                    "erau_account": erau_account or None,
                    "building_id": building_id,
                    "account_name": account_name or None,
                    "gas_meter_id": gas_meter_id or None,
                    "rate_schedule": rate_schedule or None,
                    "usage_group": usage_group or f"{building_id}|{utility_account}",
                    "cost_component": cost_component,
                    "end_use": end_use,
                    "usage_authoritative": usage_authoritative,
                    "include_in_site_eui": include_in_site_eui,
                    "active": True,
                    "notes": notes or None,
                })
                st.success(f"Added {provider} account {utility_account}.")
                st.rerun()
            except IntegrityError:
                st.error("That provider/account combination already exists. Use Edit Gas Account instead.")

    if gas_accounts.empty:
        return

    st.subheader("Edit a Gas Account")
    labels = [f"{r.id}: {r.provider} {r.utility_account} → {r.building_id}" for r in gas_accounts.itertuples(index=False)]
    selected = st.selectbox("Gas account to edit", labels, key="edit_gas_account_select")
    row = gas_accounts.iloc[labels.index(selected)]
    with st.form("edit_gas_account"):
        c1, c2, c3 = st.columns(3)
        provider_edit = c1.text_input("Provider", value=str(row.get("provider") or ""))
        account_edit = c2.text_input("Utility account", value=str(row.get("utility_account") or ""))
        building_edit = c3.selectbox("Building", building_choices, index=building_choices.index(row.get("building_id")) if row.get("building_id") in building_choices else 0)
        c1, c2, c3 = st.columns(3)
        erau_edit = c1.text_input("ERAU account", value=str(row.get("erau_account") or ""))
        meter_edit = c2.text_input("Gas meter ID", value=str(row.get("gas_meter_id") or ""))
        rate_edit = c3.text_input("Rate schedule", value=str(row.get("rate_schedule") or ""))
        name_edit = st.text_input("Account / service name", value=str(row.get("account_name") or ""))
        c1, c2, c3 = st.columns(3)
        usage_edit = c1.text_input("Usage group", value=str(row.get("usage_group") or ""))
        component_options = ["Bundled / Utility", "Utility / Delivery", "Supplier / Commodity", "Other", "Unknown"]
        component_current = row.get("cost_component") if row.get("cost_component") in component_options else "Unknown"
        component_edit = c2.selectbox("Cost component", component_options, index=component_options.index(component_current))
        end_options = ["Building", "Generator", "Process", "Unknown"]
        end_current = row.get("end_use") if row.get("end_use") in end_options else "Unknown"
        end_edit = c3.selectbox("End use", end_options, index=end_options.index(end_current))
        c1, c2, c3 = st.columns(3)
        auth_edit = c1.checkbox("Authoritative therm source", value=bool(row.get("usage_authoritative", True)))
        eui_edit = c2.checkbox("Include in Site EUI", value=bool(row.get("include_in_site_eui", True)))
        active_edit = c3.checkbox("Active", value=bool(row.get("active", True)))
        notes_edit = st.text_area("Notes", value=str(row.get("notes") or ""))
        save = st.form_submit_button("Save gas account changes")
    if save:
        update_gas_account(engine, int(row["id"]), {
            "provider": provider_edit,
            "utility_account": account_edit,
            "erau_account": erau_edit or None,
            "building_id": building_edit,
            "account_name": name_edit or None,
            "gas_meter_id": meter_edit or None,
            "rate_schedule": rate_edit or None,
            "usage_group": usage_edit or None,
            "cost_component": component_edit,
            "end_use": end_edit,
            "usage_authoritative": auth_edit,
            "include_in_site_eui": eui_edit,
            "active": active_edit,
            "notes": notes_edit or None,
        })
        st.success("Gas account updated.")
        st.rerun()


def render_gas(gas_bundle):
    st.title("Natural Gas")
    if gas_bundle is None or gas_bundle.history.empty:
        st.info("No gas history is stored yet. Upload a normalized gas workbook or CSV in the sidebar.")
        st.download_button(
            "Download blank gas history template",
            data=gas_template_excel(),
            file_name="erau_gas_history_template.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        return

    campus = gas_bundle.campus_monthly
    latest = campus.iloc[-1] if not campus.empty else None
    if latest is not None:
        st.caption(f"Latest gas month stored: {latest['billMonth']:%B %Y}")
        metric_grid([
            ("Month Gas Cost", currency(latest.get("cost")), pct(latest.get("costChangePct"))),
            ("Month Therms", number(latest.get("therms"), 1), pct(latest.get("thermsChangePct"))),
            ("Rolling 12M Gas Cost", currency(latest.get("rolling12Cost")), None),
            ("Rolling 12M Therms", number(latest.get("rolling12Therms"), 1), None),
            ("Buildings Represented", str(int(latest.get("buildingCount", 0))), None),
            ("Stored Invoice Rows", f"{len(gas_bundle.history):,}", None),
        ])

    if not campus.empty:
        c1, c2 = st.columns(2)
        with c1:
            fig = px.line(campus, x="billMonth", y="cost", markers=True, title="Monthly Natural Gas Cost")
            fig.update_yaxes(tickprefix="$", separatethousands=True)
            show_plotly(fig)
        with c2:
            fig = px.line(campus, x="billMonth", y="therms", markers=True, title="Monthly Natural Gas Use")
            fig.update_yaxes(title="Therms", separatethousands=True)
            show_plotly(fig)

    st.subheader("Building Gas Performance")
    snapshot = gas_bundle.building_snapshot
    if not snapshot.empty:
        cols = [
            "building_id", "facility_name", "use_type", "area_sqft", "rolling12Therms",
            "rolling12GasCost", "rolling12GasEUI", "rolling12GasCostPerSF", "thermsChangePct",
            "costChangePct", "gasEuiRank",
        ]
        show_dataframe(snapshot[[c for c in cols if c in snapshot.columns]], height=400)

    st.subheader("Provider / Billing Component Summary")
    st.caption(
        "Provider-level therm totals are invoice-line therms and can duplicate the same physical usage when TECO delivery and Gas South commodity bills share a usage group. "
        "Building therms and Gas EUI are de-duplicated by usage group; provider costs are always summed."
    )
    show_dataframe(gas_bundle.provider_summary, height=320)

    if not gas_bundle.fixed_charge_rows.empty:
        st.subheader("Zero-Usage Bills with Cost")
        cols = ["billMonth", "provider", "utility_account", "building_id_resolved", "account_name", "therms", "cost", "cost_component"]
        show_dataframe(gas_bundle.fixed_charge_rows[[c for c in cols if c in gas_bundle.fixed_charge_rows.columns]], height=300)

    st.download_button(
        "Download blank gas history template",
        data=gas_template_excel(),
        file_name="erau_gas_history_template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def render_total_site_eui(bundle, gas_bundle, gas_accounts):
    st.title("Total Site EUI")
    if bundle is None:
        st.info("Upload the FPL enhanced workbook to calculate electric and total Site EUI.")
        return
    combined = build_combined_site_snapshot(
        bundle.building_snapshot,
        gas_bundle.building_snapshot if gas_bundle is not None else pd.DataFrame(),
        gas_accounts,
    )
    if combined.empty:
        st.info("No building EUI data is available.")
        return

    st.caption(
        "Total Site EUI = Electric EUI + Gas EUI. Gas EUI uses de-duplicated physical therm usage. "
        "Gas accounts marked Include in Site EUI = No (for example, the Student Union generator account) are excluded from Gas EUI by default."
    )
    normal = combined[combined["eui_category"].eq("Normal Building") & combined["rolling12SiteEUI"].notna()].copy()
    if not normal.empty:
        fig = px.bar(
            normal.sort_values("rolling12SiteEUI"),
            x="rolling12SiteEUI",
            y="building_id",
            orientation="h",
            hover_data=["facility_name", "rolling12ElectricEUI", "rolling12GasEUI", "rolling12TotalEnergyCostPerSF", "siteEUIComplete"],
            title="Rolling 12-Month Total Site EUI (kBtu/sf/year)",
        )
        show_plotly(fig, height=max(500, min(1000, 24 * len(normal))))

    cols = [
        "building_id", "facility_name", "use_type", "area_sqft", "hasGasService",
        "rolling12ElectricEUI", "rolling12GasEUI", "rolling12SiteEUI", "siteEUIComplete",
        "rolling12Cost", "rolling12SiteGasCost", "rolling12TotalEnergyCost", "rolling12TotalEnergyCostPerSF",
    ]
    show_dataframe(combined[[c for c in cols if c in combined.columns]], height=480)


def render_gas_data_quality(gas_bundle):
    st.divider()
    st.header("Natural Gas Data Quality")
    if gas_bundle is None or gas_bundle.history.empty:
        st.info("No gas history stored yet.")
        return
    metric_grid([
        ("Gas invoice rows", f"{len(gas_bundle.history):,}", None),
        ("Unmapped gas accounts", f"{len(gas_bundle.unmapped_accounts):,}", None),
        ("Unregistered gas rows", f"{len(gas_bundle.unregistered_accounts):,}", None),
        ("Duplicate provider/account/month", f"{len(gas_bundle.duplicate_rows):,}", None),
        ("Zero-usage bills with cost", f"{len(gas_bundle.fixed_charge_rows):,}", None),
        ("Gas buildings", f"{gas_bundle.building_snapshot['building_id'].nunique() if not gas_bundle.building_snapshot.empty else 0:,}", None),
    ])
    if not gas_bundle.unmapped_accounts.empty:
        st.subheader("Unmapped Gas Accounts")
        show_dataframe(gas_bundle.unmapped_accounts, height=320)
    if not gas_bundle.unregistered_accounts.empty:
        st.subheader("Gas Rows Not Registered in Reference Data")
        st.caption("These rows may still map if a valid Building ID was supplied in the upload, but add the provider account in Reference Data for a durable mapping.")
        show_dataframe(gas_bundle.unregistered_accounts, height=320)
    if not gas_bundle.duplicate_rows.empty:
        st.subheader("Duplicate Gas Provider/Account/Month Rows")
        show_dataframe(gas_bundle.duplicate_rows, height=300)


def render_reference_data(engine, buildings, meters, gas_accounts, bundle=None):
    st.title("Reference Data")
    st.write("Electric bills map to buildings by meter ID when available, with the authoritative FPL account-number crosswalk as a fallback. Gas accounts remain separately managed below.")

    persistence = secret_or_env("DATABASE_URL")
    if not persistence:
        st.warning("This deployment is using the local SQLite database. Hosted Streamlit deployments should set DATABASE_URL to hosted Postgres so edits and gas history survive redeploys/restarts.")

    ref_bytes = references_to_excel(buildings, meters, gas_accounts, load_electric_accounts())
    st.download_button("Download reference tables", data=ref_bytes, file_name="erau_energy_reference_tables.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.download_button("Download blank gas history template", data=gas_template_excel(), file_name="erau_gas_history_template.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.subheader("Current Buildings")
    show_dataframe(buildings, height=320)
    st.subheader("Current Meter Mappings")
    show_dataframe(meters, height=320)
    st.subheader("Authoritative FPL Account Mappings")
    show_dataframe(load_electric_accounts(), height=420)
    st.subheader("Current Gas Accounts")
    show_dataframe(gas_accounts, height=320)

    if not admin_allowed():
        return

    tab1, tab2, tab3 = st.tabs(["Buildings", "Meters", "Gas Accounts"])
    with tab1:
        building_form(engine, buildings)
    with tab2:
        meter_form(engine, buildings, meters, bundle)
    with tab3:
        gas_account_form(engine, buildings, gas_accounts)


def render_downloads(bundle, buildings, meters, gas_bundle=None, gas_accounts=None):
    st.title("Downloads")
    if bundle is not None:
        exceptions = build_exception_table(bundle, 0.15, 0.15, 0.10)
        combined = build_combined_site_snapshot(
            bundle.building_snapshot,
            gas_bundle.building_snapshot if gas_bundle is not None else pd.DataFrame(),
            gas_accounts if gas_accounts is not None else pd.DataFrame(),
        )
        workbook = build_analysis_workbook(
            bundle,
            buildings,
            meters,
            exceptions,
            gas_bundle=gas_bundle,
            gas_accounts=gas_accounts,
            combined_snapshot=combined,
        )
        st.download_button(
            "Download analyzed workbook",
            data=workbook,
            file_name="erau_energy_dashboard_analysis.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )
        st.download_button(
            "Download building EUI snapshot CSV",
            data=bundle.building_snapshot.to_csv(index=False).encode("utf-8"),
            file_name="building_eui_snapshot.csv",
            mime="text/csv",
        )
        st.download_button(
            "Download demand snapshot CSV",
            data=bundle.building_demand_snapshot.to_csv(index=False).encode("utf-8"),
            file_name="building_demand_snapshot.csv",
            mime="text/csv",
        )
        st.download_button(
            "Download mapping exceptions CSV",
            data=bundle.unmapped_meters.to_csv(index=False).encode("utf-8"),
            file_name="unmapped_meters.csv",
            mime="text/csv",
        )
    else:
        st.info("Upload an FPL workbook to generate the combined analyzed Excel workbook.")

    if gas_bundle is not None and not gas_bundle.history.empty:
        st.download_button(
            "Download current gas history workbook",
            data=gas_history_excel(gas_bundle.history),
            file_name="ERAU_Gas_History.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )
        st.download_button(
            "Download gas history CSV",
            data=gas_bundle.history.to_csv(index=False).encode("utf-8"),
            file_name="gas_history.csv",
            mime="text/csv",
        )
        st.download_button(
            "Download gas building snapshot CSV",
            data=gas_bundle.building_snapshot.to_csv(index=False).encode("utf-8"),
            file_name="gas_building_snapshot.csv",
            mime="text/csv",
        )
    st.download_button(
        "Download blank gas history template",
        data=gas_template_excel(),
        file_name="erau_gas_history_template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def main():
    require_password()
    inject_responsive_css()
    database_url = secret_or_env("DATABASE_URL")
    engine = database_engine(database_url)

    # Idempotent additive schema upgrades. Existing FPL tables/data are preserved.
    init_database(engine, seed=True)

    buildings = load_buildings(engine, include_inactive=True)
    meters = load_meters(engine, include_inactive=True)
    gas_accounts = load_gas_accounts(engine, include_inactive=True)
    electric_accounts = load_electric_accounts()

    st.sidebar.title("⚡ ERAU Energy")
    duckdb_uploaded = st.sidebar.file_uploader(
        "Upload Energy DuckDB",
        type=["duckdb", "db"],
        key="duckdb_upload",
        help=(
            "Primary data-source option. Upload an energy.duckdb containing fpl_bills/electric_history, "
            "fpl_demand/demand_history, gas_history, and optionally electric_account_mapping. "
            "The dashboard queries the uploaded database directly for this session."
        ),
    )
    st.sidebar.caption("Preferred: one DuckDB file containing the current energy history.")
    with st.sidebar.expander("Excel import fallback", expanded=False):
        uploaded = st.file_uploader(
            "Upload monthly FPL bill file",
            type=["xls", "xlsx"],
            key="fpl_upload",
            help=(
                "Optional fallback/admin import. Upload either the master FPL_Combined_Bills workbook "
                "or just the newest monthly FPL XLS/XLSX."
            ),
        )
    st.sidebar.divider()
    gas_uploaded = st.sidebar.file_uploader(
        "Upload gas history workbook",
        type=["xlsx", "csv"],
        key="gas_upload",
        help="Keep one cumulative file and append each month. Required columns: Billing Month, Utility Account, Cost, Therms.",
    )

    page = st.sidebar.radio(
        "Dashboard",
        [
            "Overview", "Cost & Usage", "Demand", "Building EUI", "Variance & Exceptions",
            "Natural Gas", "Total Site EUI", "Data Quality", "Reference Data", "Downloads",
        ],
    )
    render_logout_button()

    # DuckDB is the primary session data source when supplied. Excel/database
    # persistence remains available as an admin/fallback workflow.
    duckdb_result = None
    if duckdb_uploaded is not None:
        try:
            db_raw = duckdb_uploaded.getvalue()
            duckdb_result = load_uploaded_duckdb(db_raw, duckdb_uploaded.name)
            st.sidebar.success(f"DuckDB loaded: {len(duckdb_result.tables)} tables")
            st.sidebar.caption(", ".join(duckdb_result.tables[:8]))
            if len(duckdb_result.tables) > 8:
                st.sidebar.caption(f"+ {len(duckdb_result.tables) - 8} more tables")
            if not duckdb_result.electric_accounts.empty:
                electric_accounts = duckdb_result.electric_accounts
                st.sidebar.caption(
                    f"Account mapping: {electric_accounts['account_number'].nunique():,} FPL accounts from DuckDB"
                )
            if not duckdb_result.gas_accounts.empty:
                gas_accounts = duckdb_result.gas_accounts
                st.sidebar.caption(
                    f"Gas mapping: {gas_accounts['utility_account'].nunique():,} utility accounts from DuckDB"
                )
        except Exception as exc:
            st.error(f"Could not open uploaded DuckDB: {exc}")
            duckdb_result = None

    # Gas is persisted independently when imported from Excel, but an uploaded
    # DuckDB gas_history table takes precedence for the current session.
    if gas_uploaded is not None and duckdb_result is None:
        try:
            gas_raw = gas_uploaded.getvalue()
            gas_result = load_gas_file(
                gas_raw,
                gas_uploaded.name,
                gas_accounts=gas_accounts,
            )
            gas_hash = hashlib.sha256(gas_raw).hexdigest()
            if st.session_state.get("saved_gas_upload_hash") != gas_hash:
                stats = upsert_gas_history(engine, gas_result.rows, gas_uploaded.name)
                st.session_state["saved_gas_upload_hash"] = gas_hash
                st.session_state["gas_save_stats"] = stats
            month_min = gas_result.rows["billMonth"].min()
            month_max = gas_result.rows["billMonth"].max()
            st.sidebar.success(f"Gas: synced {len(gas_result.rows):,} history rows")
            if pd.notna(month_min) and pd.notna(month_max):
                st.sidebar.caption(f"{month_min:%b %Y} through {month_max:%b %Y}")
            if st.session_state.get("gas_save_stats"):
                gs = st.session_state["gas_save_stats"]
                st.sidebar.caption(
                    f"Database sync: {gs.get('inserted', 0):,} inserted · {gs.get('updated', 0):,} updated"
                )
        except Exception as exc:
            st.error(f"Could not process gas upload: {exc}")

    if duckdb_result is not None and not duckdb_result.gas_history.empty:
        gas_history = duckdb_result.gas_history
        st.sidebar.caption(f"Gas from DuckDB: {len(gas_history):,} rows")
    else:
        gas_history = load_gas_history(engine)

    gas_bundle = run_gas_analytics(gas_history, gas_accounts, buildings) if not gas_history.empty else None
    if gas_bundle is not None and duckdb_result is None:
        st.sidebar.caption(
            f"Gas history: {len(gas_history):,} stored rows · {gas_history['utility_account'].nunique()} accounts"
        )

    bundle = None
    if duckdb_result is not None:
        electric_history = duckdb_result.electric_history
        demand_history = duckdb_result.demand_history
        if not electric_history.empty:
            electric_history = normalize_bill_history(electric_history)
            bundle = run_analytics(electric_history, buildings, meters, demand_history, electric_accounts)
            st.sidebar.success(f"FPL from DuckDB: {len(electric_history):,} bill rows")
            st.sidebar.caption(
                f"{electric_history['accountNumber'].nunique()} accounts · "
                f"latest {electric_history['dateBilled'].max():%B %d, %Y}"
            )
            if demand_history is not None and not demand_history.empty:
                st.sidebar.caption(f"Demand from DuckDB: {len(demand_history):,} rows")
        else:
            st.sidebar.warning("Uploaded DuckDB has no supported FPL bill table.")
    elif uploaded is not None:
        try:
            raw = uploaded.getvalue()
            result = load_monthly_workbook(raw, uploaded.name)

            upload_hash = hashlib.sha256(raw).hexdigest()

            if st.session_state.get("saved_electric_upload_hash") != upload_hash:
                electric_save = upsert_electric_history(engine, result.bills, uploaded.name)
                st.session_state["saved_electric_upload_hash"] = upload_hash
                st.session_state["electric_save_stats"] = electric_save

            if not result.demand.empty and st.session_state.get("saved_demand_upload_hash") != upload_hash:
                demand_save = upsert_demand_history(engine, result.demand, uploaded.name)
                st.session_state["saved_demand_upload_hash"] = upload_hash
                st.session_state["demand_save_stats"] = demand_save

            electric_history = load_electric_history(engine)
            electric_history = normalize_bill_history(electric_history) if not electric_history.empty else result.bills
            demand_history = load_demand_history(engine)
            bundle = run_analytics(electric_history, buildings, meters, demand_history, electric_accounts)

            st.sidebar.success(f"FPL database: {len(electric_history):,} stored bill rows")
            st.sidebar.caption(
                f"{electric_history['accountNumber'].nunique()} accounts · "
                f"{electric_history['fullMeterID'].replace('', pd.NA).nunique()} meters"
            )
            if st.session_state.get("electric_save_stats"):
                es = st.session_state["electric_save_stats"]
                st.sidebar.caption(
                    f"Electric sync: {es.get('inserted', 0):,} inserted · "
                    f"{es.get('updated', 0):,} updated"
                )
            if not result.demand.empty:
                st.sidebar.caption(f"{len(result.demand):,} demand records extracted this upload")
        except Exception as exc:
            st.error(f"Could not process workbook: {exc}")
            bundle = None
    else:
        electric_history = load_electric_history(engine)
        demand_history = load_demand_history(engine)
        if not electric_history.empty:
            electric_history = normalize_bill_history(electric_history)
            bundle = run_analytics(electric_history, buildings, meters, demand_history, electric_accounts)
            st.sidebar.success(f"FPL database: {len(electric_history):,} stored bill rows")
            st.sidebar.caption(
                f"Latest stored bill: {electric_history['dateBilled'].max():%B %d, %Y}"
            )
        else:
            st.sidebar.info("Upload energy.duckdb, or use the Excel import fallback to seed stored history.")

    if page == "Reference Data":
        render_reference_data(engine, buildings, meters, gas_accounts, bundle)
        return

    if page == "Natural Gas":
        render_gas(gas_bundle)
        return

    if page == "Total Site EUI":
        render_total_site_eui(bundle, gas_bundle, gas_accounts)
        return

    if page == "Data Quality":
        if bundle is not None:
            render_data_quality(bundle)
        else:
            st.title("Data Quality")
            st.info("Upload energy.duckdb in the sidebar, or use the Excel fallback import, to run data-quality checks.")
        render_gas_data_quality(gas_bundle)
        return

    if page == "Downloads":
        render_downloads(bundle, buildings, meters, gas_bundle, gas_accounts)
        return

    if bundle is None:
        st.title(APP_TITLE)
        st.write("Upload your current energy.duckdb in the sidebar for the cleanest workflow. The dashboard reads FPL, demand, gas, and account mappings directly from that database. Excel remains available as a fallback import path.")
        st.subheader("Monthly workflow")
        st.markdown(
            "1. Upload `energy.duckdb` in the Streamlit sidebar.\n"
            "2. The app reads FPL bill history, demand history, gas history, and FPL account mappings directly from supported DuckDB tables.\n"
            "3. Update the DuckDB locally each month, then replace the uploaded database with the current file.\n"
            "4. Use the Excel import fallback only when you need to seed or repair the dashboard's built-in storage.\n"
            "5. Review Natural Gas and Total Site EUI for gas cost, therms, Gas EUI, and combined Site EUI.\n"
            "6. Add new buildings, meters, and gas accounts in Reference Data when needed."
        )
        st.download_button(
            "Download blank gas history template",
            data=gas_template_excel(),
            file_name="erau_gas_history_template.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        return

    if page == "Overview":
        render_overview(bundle)
    elif page == "Cost & Usage":
        render_cost_usage(bundle)
    elif page == "Demand":
        render_demand(bundle)
    elif page == "Building EUI":
        render_eui(bundle)
    elif page == "Variance & Exceptions":
        render_variance(bundle)


if __name__ == "__main__":
    main()
