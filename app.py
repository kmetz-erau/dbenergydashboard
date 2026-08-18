from __future__ import annotations

import io
import os
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy.exc import IntegrityError

from src.analytics import build_exception_table, run_analytics
from src.exporter import build_analysis_workbook
from src.loader import load_monthly_workbook
from src.reference_io import references_to_excel
from src.storage import (
    add_building,
    add_meter,
    get_engine,
    init_database,
    load_buildings,
    load_meters,
    update_building,
    update_meter,
)

APP_TITLE = "ERAU Energy Dashboard"

st.set_page_config(page_title=APP_TITLE, page_icon="⚡", layout="wide")


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
    engine = get_engine(database_url)
    init_database(engine, seed=True)
    return engine


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
    cols = st.columns(6)
    if latest is None:
        for col, label in zip(cols, ["Month Cost", "Month kWh", "Effective $/kWh", "MoM Cost", "MoM Usage", "Buildings"]):
            col.metric(label, "—")
        return

    cols[0].metric("Month Cost", currency(latest.get("cost")), pct(latest.get("costChangePct")))
    cols[1].metric("Month kWh", number(latest.get("kWh")), pct(latest.get("kWhChangePct")))
    cols[2].metric("Rolling 12M Cost", currency(latest.get("rolling12Cost")))
    cols[3].metric("Rolling 12M kWh", number(latest.get("rolling12KWh")))
    cols[4].metric("Rolling 12M Electric EUI", number(latest.get("rolling12ElectricEUI"), 1))
    cols[5].metric("Buildings Represented", int(latest.get("buildingCount", 0)))


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
        st.info("No mapped building data is available yet. Review meter mappings in Data Quality / Reference Data.")
        return

    c1, c2 = st.columns(2)
    with c1:
        fig = px.line(campus, x="billMonth", y="cost", markers=True, title="Monthly Electricity Cost")
        fig.update_yaxes(tickprefix="$", separatethousands=True)
        st.plotly_chart(fig, use_container_width=True)
    with c2:
        fig = px.line(campus, x="billMonth", y="kWh", markers=True, title="Monthly Electricity Use")
        fig.update_yaxes(separatethousands=True)
        st.plotly_chart(fig, use_container_width=True)

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
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("Latest Building Snapshot")
        columns = [
            "building_id", "facility_name", "use_type", "area_sqft", "rolling12KWh", "rolling12Cost",
            "rolling12ElectricEUI", "rolling12CostPerSF", "effectiveCostPerKWh", "peerRank",
        ]
        st.dataframe(snapshot[[c for c in columns if c in snapshot.columns]], use_container_width=True, hide_index=True)


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
    st.plotly_chart(fig, use_container_width=True)

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
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("Rate Schedule Summary")
    allocated = bundle.allocated_bills.copy()
    if not allocated.empty:
        rate_summary = (
            allocated.groupby("rate_schedule", dropna=False)
            .agg(cost=("allocatedCost", "sum"), kWh=("allocatedKWh", "sum"), meters=("fullMeterID", "nunique"), buildings=("building_id", "nunique"))
            .reset_index()
        )
        rate_summary["effectiveCostPerKWh"] = rate_summary["cost"] / rate_summary["kWh"].where(rate_summary["kWh"] > 0)
        st.dataframe(rate_summary.sort_values("cost", ascending=False), use_container_width=True, hide_index=True)


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
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("Building Detail")
    choices = sorted(snapshot["building_id"].dropna().unique().tolist())
    selected = st.selectbox("Select building", choices, key="eui_building")
    row = snapshot[snapshot["building_id"].eq(selected)].iloc[0]
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Area", number(row.get("area_sqft")) + " sf" if pd.notna(row.get("area_sqft")) else "—")
    c2.metric("Rolling 12M kWh", number(row.get("rolling12KWh")))
    c3.metric("Rolling 12M Cost", currency(row.get("rolling12Cost")))
    c4.metric("Electric EUI", number(row.get("rolling12ElectricEUI"), 1))
    c5.metric("Cost / sf", f"${row.get('rolling12CostPerSF'):.2f}" if pd.notna(row.get("rolling12CostPerSF")) else "—")
    st.caption("Electric EUI is electricity-only site energy intensity: kWh × 3.412 ÷ gross floor area. Special energy facilities are separated from normal-building peer rankings.")

    detail = bundle.building_monthly[bundle.building_monthly["building_id"].eq(selected)]
    fig = px.line(detail, x="billMonth", y="electricEUI", markers=True, title="Monthly Electric EUI")
    st.plotly_chart(fig, use_container_width=True)


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
        st.dataframe(exceptions, use_container_width=True, hide_index=True)

    st.subheader("Account-Level Bill Variance")
    cols = [
        "accountNumber", "accountName", "serviceAddress", "fullMeterID", "dateBilled",
        "totalBillAmount", "previousBillAmount", "billAmountChange", "billAmountChangePct",
        "kWh", "previousKWh", "kWhChange", "kWhChangePct", "kWhPerDayChangePct", "costPerDayChangePct",
    ]
    latest_accounts = bundle.bills.sort_values(["accountNumber", "dateBilled"]).groupby("accountNumber", as_index=False).tail(1)
    st.dataframe(latest_accounts[[c for c in cols if c in latest_accounts.columns]], use_container_width=True, hide_index=True)


def render_data_quality(bundle):
    st.title("Data Quality")
    bills = bundle.bills
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Bill rows", len(bills))
    c2.metric("Accounts", bills["accountNumber"].nunique())
    c3.metric("Meters in history", bills["fullMeterID"].replace("", pd.NA).nunique())
    c4.metric("Unmapped meters", bundle.unmapped_meters["fullMeterID"].nunique() if not bundle.unmapped_meters.empty else 0)

    if not bundle.unmapped_meters.empty:
        st.subheader("Unmapped FPL Meters")
        st.dataframe(bundle.unmapped_meters, use_container_width=True, hide_index=True)

    if not bundle.meter_suggestions.empty:
        st.subheader("Suggested New Meter Mappings")
        st.caption("Suggestions are not applied automatically. Confirm them in Reference Data before they affect EUI.")
        st.dataframe(bundle.meter_suggestions, use_container_width=True, hide_index=True)

    if not bundle.shared_meter_issues.empty:
        st.subheader("Shared Meter Allocation Issues")
        st.warning("These meter mappings are excluded from building EUI until all building allocations are populated and sum to 100%.")
        st.dataframe(bundle.shared_meter_issues, use_container_width=True, hide_index=True)

    if not bundle.missing_area.empty:
        st.subheader("Buildings Missing Floor Area")
        st.dataframe(bundle.missing_area, use_container_width=True, hide_index=True)

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


def render_reference_data(engine, buildings, meters, bundle=None):
    st.title("Reference Data")
    st.write("Add buildings and meters here. These records drive meter-to-building allocation, rate schedule analysis, square footage, and EUI.")

    persistence = secret_or_env("DATABASE_URL")
    if not persistence:
        st.warning("This deployment is using the local SQLite database. It is persistent on your computer, but hosted Streamlit deployments should set DATABASE_URL to hosted Postgres so edits survive redeploys/restarts.")

    ref_bytes = references_to_excel(buildings, meters)
    st.download_button("Download reference tables", data=ref_bytes, file_name="erau_energy_reference_tables.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.subheader("Current Buildings")
    st.dataframe(buildings, use_container_width=True, hide_index=True)
    st.subheader("Current Meter Mappings")
    st.dataframe(meters, use_container_width=True, hide_index=True)

    if not admin_allowed():
        return

    tab1, tab2 = st.tabs(["Buildings", "Meters"])
    with tab1:
        building_form(engine, buildings)
    with tab2:
        meter_form(engine, buildings, meters, bundle)


def render_downloads(bundle, buildings, meters):
    st.title("Downloads")
    exceptions = build_exception_table(bundle, 0.15, 0.15, 0.10)
    workbook = build_analysis_workbook(bundle, buildings, meters, exceptions)
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
        "Download mapping exceptions CSV",
        data=bundle.unmapped_meters.to_csv(index=False).encode("utf-8"),
        file_name="unmapped_meters.csv",
        mime="text/csv",
    )


def main():
    database_url = secret_or_env("DATABASE_URL")
    engine = database_engine(database_url)
    buildings = load_buildings(engine, include_inactive=True)
    meters = load_meters(engine, include_inactive=True)

    st.sidebar.title("⚡ ERAU Energy")
    uploaded = st.sidebar.file_uploader("Upload monthly enhanced FPL workbook", type=["xlsx"])
    page = st.sidebar.radio(
        "Dashboard",
        ["Overview", "Cost & Usage", "Building EUI", "Variance & Exceptions", "Data Quality", "Reference Data", "Downloads"],
    )

    bundle = None
    if uploaded is not None:
        try:
            raw = uploaded.getvalue()
            result = load_monthly_workbook(raw, uploaded.name)
            bundle = run_analytics(result.bills, buildings, meters)
            st.sidebar.success(f"Loaded {len(result.bills):,} bill rows")
            st.sidebar.caption(f"{result.bills['accountNumber'].nunique()} accounts · {result.bills['fullMeterID'].replace('', pd.NA).nunique()} meters")
        except Exception as exc:
            st.error(f"Could not process workbook: {exc}")
            bundle = None
    else:
        st.sidebar.info("Upload the latest monthly workbook to populate analytics.")

    if page == "Reference Data":
        render_reference_data(engine, buildings, meters, bundle)
        return

    if bundle is None:
        st.title(APP_TITLE)
        st.write("Upload the enhanced FPL workbook in the sidebar. You can manage buildings and meters at any time from **Reference Data**.")
        st.subheader("Monthly workflow")
        st.markdown(
            "1. Upload the latest enhanced FPL `.xlsx` file.\n"
            "2. The app validates Bill History and reconstructs FPL meter IDs.\n"
            "3. Meters map to buildings, square footage, utility and rate schedule.\n"
            "4. The dashboard calculates cost, usage, variance, kWh/sf, $/sf and electric EUI.\n"
            "5. New/unmapped meters are surfaced for review and can be added in Reference Data."
        )
        return

    if page == "Overview":
        render_overview(bundle)
    elif page == "Cost & Usage":
        render_cost_usage(bundle)
    elif page == "Building EUI":
        render_eui(bundle)
    elif page == "Variance & Exceptions":
        render_variance(bundle)
    elif page == "Data Quality":
        render_data_quality(bundle)
    elif page == "Downloads":
        render_downloads(bundle, buildings, meters)


if __name__ == "__main__":
    main()
