from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

KBTU_PER_THERM = 100.0


@dataclass
class GasAnalyticsBundle:
    history: pd.DataFrame
    enriched_history: pd.DataFrame
    usage_group_monthly: pd.DataFrame
    building_monthly: pd.DataFrame
    building_snapshot: pd.DataFrame
    campus_monthly: pd.DataFrame
    provider_summary: pd.DataFrame
    unmapped_accounts: pd.DataFrame
    unregistered_accounts: pd.DataFrame
    fixed_charge_rows: pd.DataFrame
    duplicate_rows: pd.DataFrame


def _active(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "active" in out.columns:
        out = out[out["active"].fillna(True).astype(bool)].copy()
    return out


def enrich_gas_history(
    history: pd.DataFrame,
    gas_accounts: pd.DataFrame,
    buildings: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if history is None or history.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    h = history.copy()
    h["provider"] = h["provider"].astype(str).str.strip().str.upper()
    h["utility_account"] = h["utility_account"].astype(str).str.strip()
    h["billMonth"] = pd.to_datetime(h["billMonth"], errors="coerce").dt.to_period("M").dt.to_timestamp()
    h["therms"] = pd.to_numeric(h["therms"], errors="coerce")
    h["cost"] = pd.to_numeric(h["cost"], errors="coerce")

    accounts = _active(gas_accounts)
    if accounts.empty:
        accounts = pd.DataFrame(columns=[
            "provider", "utility_account", "building_id", "erau_account", "account_name",
            "gas_meter_id", "rate_schedule", "usage_group", "cost_component", "end_use",
            "usage_authoritative", "include_in_site_eui", "active", "notes",
        ])
    else:
        accounts = accounts.copy()
        accounts["provider"] = accounts["provider"].astype(str).str.strip().str.upper()
        accounts["utility_account"] = accounts["utility_account"].astype(str).str.strip()

    ref_cols = [
        "provider", "utility_account", "building_id", "erau_account", "account_name", "gas_meter_id",
        "rate_schedule", "usage_group", "cost_component", "end_use", "usage_authoritative",
        "include_in_site_eui", "notes",
    ]
    ref = accounts[[c for c in ref_cols if c in accounts.columns]].copy()
    rename_ref = {c: f"ref_{c}" for c in ref.columns if c not in {"provider", "utility_account"}}
    ref = ref.rename(columns=rename_ref)

    out = h.merge(ref, on=["provider", "utility_account"], how="left", indicator="accountReferenceMatch")
    out["registeredAccount"] = out["accountReferenceMatch"].eq("both")

    building_ref = _active(buildings)
    valid_buildings = set(building_ref.get("building_id", pd.Series(dtype=str)).dropna().astype(str))
    uploaded_building = out.get("building_id", pd.Series(index=out.index, dtype=object))
    registered_building = out.get("ref_building_id", pd.Series(index=out.index, dtype=object))
    out["building_id_resolved"] = registered_building.where(registered_building.notna(), uploaded_building)
    out.loc[~out["building_id_resolved"].isin(valid_buildings), "building_id_resolved"] = np.nan

    for field in ["erau_account", "account_name", "gas_meter_id", "rate_schedule", "usage_group", "cost_component", "end_use"]:
        raw = out.get(field, pd.Series(index=out.index, dtype=object))
        ref_field = out.get(f"ref_{field}", pd.Series(index=out.index, dtype=object))
        out[f"resolved_{field}"] = ref_field.where(ref_field.notna(), raw)

    out["usage_group"] = out["resolved_usage_group"].fillna(
        out["building_id_resolved"].fillna("UNMAPPED").astype(str)
        + "|" + out["utility_account"].astype(str)
    )
    out["end_use"] = out["resolved_end_use"].fillna("Unknown")
    out["cost_component"] = out["resolved_cost_component"].fillna("Unknown")
    out["usage_authoritative"] = out.get("ref_usage_authoritative", False)
    out["usage_authoritative"] = out["usage_authoritative"].fillna(False).astype(bool)
    out["include_in_site_eui"] = out.get("ref_include_in_site_eui", True)
    out["include_in_site_eui"] = out["include_in_site_eui"].fillna(True).astype(bool)
    out["lineCostPerTherm"] = out["cost"] / out["therms"].where(out["therms"] > 0)

    if not building_ref.empty:
        bcols = ["building_id", "property_id", "campus", "facility_name", "use_type", "area_sqft", "eui_category"]
        bref = building_ref[[c for c in bcols if c in building_ref.columns]].rename(columns={"building_id": "building_id_resolved"})
        out = out.merge(bref, on="building_id_resolved", how="left")

    unmapped = out[out["building_id_resolved"].isna()].copy()
    if not unmapped.empty:
        unmapped = (
            unmapped.groupby(["provider", "utility_account"], dropna=False)
            .agg(
                accountName=("account_name", "first"),
                firstMonth=("billMonth", "min"),
                lastMonth=("billMonth", "max"),
                invoiceRows=("billMonth", "count"),
                totalCost=("cost", "sum"),
            )
            .reset_index()
        )

    unregistered = out[~out["registeredAccount"]].copy()
    if not unregistered.empty:
        unregistered = unregistered[[
            "provider", "utility_account", "building_id_resolved", "billMonth", "therms", "cost"
        ]].drop_duplicates().reset_index(drop=True)

    return out, unmapped, unregistered


def build_usage_group_monthly(enriched: pd.DataFrame) -> pd.DataFrame:
    if enriched is None or enriched.empty:
        return pd.DataFrame()

    rows = []
    grouped = enriched[enriched["building_id_resolved"].notna()].groupby(
        ["building_id_resolved", "usage_group", "billMonth"], dropna=False
    )
    for (building_id, usage_group, bill_month), grp in grouped:
        authoritative = grp[grp["usage_authoritative"] & grp["therms"].notna()]
        usage_rows = authoritative if not authoritative.empty else grp[grp["therms"].notna()]
        therms = usage_rows["therms"].max() if not usage_rows.empty else np.nan
        usage_source = ", ".join(sorted(set(usage_rows["provider"].dropna().astype(str)))) if not usage_rows.empty else None
        rows.append({
            "building_id": building_id,
            "usage_group": usage_group,
            "billMonth": bill_month,
            "therms": therms,
            "cost": grp["cost"].sum(min_count=1),
            "providerCount": grp["provider"].nunique(),
            "providers": ", ".join(sorted(set(grp["provider"].dropna().astype(str)))),
            "usageSource": usage_source,
            "invoiceRows": len(grp),
            "include_in_site_eui": bool(grp["include_in_site_eui"].fillna(True).all()),
            "end_use": ", ".join(sorted(set(grp["end_use"].dropna().astype(str)))),
            "cost_component": ", ".join(sorted(set(grp["cost_component"].dropna().astype(str)))),
        })
    return pd.DataFrame(rows).sort_values(["building_id", "billMonth", "usage_group"]).reset_index(drop=True)


def build_building_gas_monthly(groups: pd.DataFrame, buildings: pd.DataFrame) -> pd.DataFrame:
    if groups is None or groups.empty:
        return pd.DataFrame()

    rows = []
    for (building_id, month), grp in groups.groupby(["building_id", "billMonth"], dropna=False):
        eligible = grp[grp["include_in_site_eui"]]
        generator = grp[grp["end_use"].astype(str).str.contains("generator", case=False, na=False)]
        rows.append({
            "building_id": building_id,
            "billMonth": month,
            "therms": grp["therms"].sum(min_count=1),
            "siteEuiTherms": eligible["therms"].sum(min_count=1),
            "generatorTherms": generator["therms"].sum(min_count=1),
            "cost": grp["cost"].sum(min_count=1),
            "siteGasCost": eligible["cost"].sum(min_count=1),
            "usageGroupCount": grp["usage_group"].nunique(),
            "providerCount": grp["providers"].nunique(),
        })
    monthly = pd.DataFrame(rows)

    b = _active(buildings)
    if not b.empty:
        cols = ["building_id", "property_id", "campus", "facility_name", "use_type", "area_sqft", "eui_category"]
        monthly = monthly.merge(b[[c for c in cols if c in b.columns]], on="building_id", how="left")

    monthly["gasEUI"] = monthly["siteEuiTherms"] * KBTU_PER_THERM / monthly["area_sqft"].where(monthly["area_sqft"] > 0)
    monthly["gasCostPerSF"] = monthly["siteGasCost"] / monthly["area_sqft"].where(monthly["area_sqft"] > 0)
    monthly["effectiveGasCostPerTherm"] = monthly["cost"] / monthly["therms"].where(monthly["therms"] > 0)
    monthly = monthly.sort_values(["building_id", "billMonth"]).reset_index(drop=True)

    g = monthly.groupby("building_id", group_keys=False)
    monthly["previousTherms"] = g["therms"].shift(1)
    monthly["previousCost"] = g["cost"].shift(1)
    monthly["previousGasEUI"] = g["gasEUI"].shift(1)
    monthly["thermsChangePct"] = (monthly["therms"] - monthly["previousTherms"]) / monthly["previousTherms"].replace(0, np.nan)
    monthly["costChangePct"] = (monthly["cost"] - monthly["previousCost"]) / monthly["previousCost"].replace(0, np.nan)
    monthly["gasEUIChangePct"] = (monthly["gasEUI"] - monthly["previousGasEUI"]) / monthly["previousGasEUI"].replace(0, np.nan)
    monthly["rolling12Therms"] = g["therms"].rolling(12, min_periods=1).sum().reset_index(level=0, drop=True)
    monthly["rolling12SiteEuiTherms"] = g["siteEuiTherms"].rolling(12, min_periods=1).sum().reset_index(level=0, drop=True)
    monthly["rolling12GasCost"] = g["cost"].rolling(12, min_periods=1).sum().reset_index(level=0, drop=True)
    monthly["rolling12SiteGasCost"] = g["siteGasCost"].rolling(12, min_periods=1).sum().reset_index(level=0, drop=True)
    monthly["rolling12Months"] = g.cumcount().add(1).clip(upper=12)
    monthly["rolling12GasEUI"] = monthly["rolling12SiteEuiTherms"] * KBTU_PER_THERM / monthly["area_sqft"].where(monthly["area_sqft"] > 0)
    monthly["rolling12GasCostPerSF"] = monthly["rolling12SiteGasCost"] / monthly["area_sqft"].where(monthly["area_sqft"] > 0)
    monthly["rolling12Complete"] = monthly["rolling12Months"].ge(12)
    return monthly


def build_gas_snapshot(monthly: pd.DataFrame) -> pd.DataFrame:
    if monthly is None or monthly.empty:
        return pd.DataFrame()
    out = monthly.sort_values(["building_id", "billMonth"]).groupby("building_id", as_index=False).tail(1).copy()
    eligible = out["rolling12GasEUI"].notna() & out["eui_category"].eq("Normal Building")
    out["gasEuiRank"] = np.nan
    out.loc[eligible, "gasEuiRank"] = out.loc[eligible, "rolling12GasEUI"].rank(method="min", ascending=False)
    return out.sort_values("rolling12GasCost", ascending=False, na_position="last").reset_index(drop=True)


def build_gas_campus_monthly(monthly: pd.DataFrame) -> pd.DataFrame:
    if monthly is None or monthly.empty:
        return pd.DataFrame()
    rows = []
    for month, grp in monthly.groupby("billMonth"):
        rows.append({
            "billMonth": month,
            "therms": grp["therms"].sum(min_count=1),
            "siteEuiTherms": grp["siteEuiTherms"].sum(min_count=1),
            "cost": grp["cost"].sum(min_count=1),
            "buildingCount": grp["building_id"].nunique(),
            "gasEuiBuildingCount": grp["gasEUI"].notna().sum(),
        })
    out = pd.DataFrame(rows).sort_values("billMonth").reset_index(drop=True)
    out["thermsChangePct"] = out["therms"].pct_change(fill_method=None)
    out["costChangePct"] = out["cost"].pct_change(fill_method=None)
    out["rolling12Therms"] = out["therms"].rolling(12, min_periods=1).sum()
    out["rolling12Cost"] = out["cost"].rolling(12, min_periods=1).sum()
    out["rolling12Months"] = np.minimum(np.arange(1, len(out) + 1), 12)
    return out


def build_provider_summary(enriched: pd.DataFrame) -> pd.DataFrame:
    if enriched is None or enriched.empty:
        return pd.DataFrame()
    summary = (
        enriched.groupby(["billMonth", "provider"], dropna=False)
        .agg(invoiceTherms=("therms", "sum"), cost=("cost", "sum"), accounts=("utility_account", "nunique"))
        .reset_index()
    )
    summary["invoiceCostPerTherm"] = summary["cost"] / summary["invoiceTherms"].where(summary["invoiceTherms"] > 0)
    return summary


def run_gas_analytics(history: pd.DataFrame, gas_accounts: pd.DataFrame, buildings: pd.DataFrame) -> GasAnalyticsBundle:
    history = history.copy() if history is not None else pd.DataFrame()
    enriched, unmapped, unregistered = enrich_gas_history(history, gas_accounts, buildings)
    groups = build_usage_group_monthly(enriched)
    monthly = build_building_gas_monthly(groups, buildings)
    snapshot = build_gas_snapshot(monthly)
    campus = build_gas_campus_monthly(monthly)
    provider = build_provider_summary(enriched)
    fixed = enriched[(enriched["therms"].fillna(0).eq(0)) & (enriched["cost"].fillna(0).gt(0))].copy() if not enriched.empty else pd.DataFrame()
    dup_subset = [c for c in ["provider", "utility_account", "billMonth"] if c in history.columns]
    duplicate_rows = history[history.duplicated(subset=dup_subset, keep=False)].copy() if dup_subset else pd.DataFrame()
    return GasAnalyticsBundle(
        history=history,
        enriched_history=enriched,
        usage_group_monthly=groups,
        building_monthly=monthly,
        building_snapshot=snapshot,
        campus_monthly=campus,
        provider_summary=provider,
        unmapped_accounts=unmapped,
        unregistered_accounts=unregistered,
        fixed_charge_rows=fixed,
        duplicate_rows=duplicate_rows,
    )


def build_combined_site_snapshot(electric_snapshot: pd.DataFrame, gas_snapshot: pd.DataFrame, gas_accounts: pd.DataFrame) -> pd.DataFrame:
    if electric_snapshot is None or electric_snapshot.empty:
        return pd.DataFrame()
    e = electric_snapshot.copy()
    gas_cols = [
        "building_id", "rolling12Therms", "rolling12SiteEuiTherms", "rolling12GasCost",
        "rolling12SiteGasCost", "rolling12GasEUI", "rolling12GasCostPerSF", "rolling12Months",
        "rolling12Complete",
    ]
    g = gas_snapshot[[c for c in gas_cols if c in gas_snapshot.columns]].copy() if gas_snapshot is not None and not gas_snapshot.empty else pd.DataFrame(columns=gas_cols)
    out = e.merge(g, on="building_id", how="left", suffixes=("", "_gas"))

    active_accounts = _active(gas_accounts)
    gas_service_buildings = set(active_accounts.get("building_id", pd.Series(dtype=str)).dropna().astype(str))
    out["hasGasService"] = out["building_id"].isin(gas_service_buildings)
    out["gasEUIForSite"] = out["rolling12GasEUI"]
    out.loc[~out["hasGasService"], "gasEUIForSite"] = 0.0
    out["rolling12SiteEUI"] = out["rolling12ElectricEUI"] + out["gasEUIForSite"]
    out.loc[out["hasGasService"] & out["rolling12GasEUI"].isna(), "rolling12SiteEUI"] = np.nan

    electric_complete = out.get("rolling12Complete", False)
    gas_complete = out.get("rolling12Complete_gas", out.get("rolling12Complete_y", False))
    if not isinstance(electric_complete, pd.Series):
        electric_complete = pd.Series(False, index=out.index)
    if not isinstance(gas_complete, pd.Series):
        gas_complete = pd.Series(False, index=out.index)
    electric_complete = electric_complete.astype("boolean").fillna(False).astype(bool)
    gas_complete = gas_complete.astype("boolean").fillna(False).astype(bool)
    out["siteEUIComplete"] = electric_complete & (~out["hasGasService"] | gas_complete)
    out["rolling12TotalEnergyCost"] = out["rolling12Cost"] + out["rolling12SiteGasCost"].fillna(0)
    out["rolling12TotalEnergyCostPerSF"] = out["rolling12TotalEnergyCost"] / out["area_sqft"].where(out["area_sqft"] > 0)
    return out
