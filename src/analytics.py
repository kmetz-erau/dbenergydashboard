from __future__ import annotations

from dataclasses import dataclass
import re

import numpy as np
import pandas as pd

from .loader import normalize_meter_id
from .electric_accounts import normalize_account_number

KBTU_PER_KWH = 3.412


@dataclass
class AnalyticsBundle:
    bills: pd.DataFrame
    allocated_bills: pd.DataFrame
    building_monthly: pd.DataFrame
    building_snapshot: pd.DataFrame
    campus_monthly: pd.DataFrame
    unmapped_meters: pd.DataFrame
    shared_meter_issues: pd.DataFrame
    meter_suggestions: pd.DataFrame
    missing_area: pd.DataFrame
    duplicate_bills: pd.DataFrame
    demand_details: pd.DataFrame
    allocated_demand: pd.DataFrame
    building_demand_monthly: pd.DataFrame
    building_demand_snapshot: pd.DataFrame
    unmapped_demand_meters: pd.DataFrame


def prepare_references(buildings: pd.DataFrame, meters: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    b = buildings.copy()
    m = meters.copy()
    if not b.empty:
        b["building_id"] = b["building_id"].astype(str).str.strip().str.replace(r"^Building(?=\d)", "Building ", regex=True)
        b["area_sqft"] = pd.to_numeric(b["area_sqft"], errors="coerce")
        if "active" in b.columns:
            b = b[b["active"].fillna(True).astype(bool)].copy()
    if not m.empty:
        m["building_id"] = m["building_id"].astype(str).str.strip().str.replace(r"^Building(?=\d)", "Building ", regex=True)
        m["meter_id"] = m["meter_id"].map(normalize_meter_id)
        m["allocation_pct"] = pd.to_numeric(m["allocation_pct"], errors="coerce")
        if "active" in m.columns:
            m = m[m["active"].fillna(True).astype(bool)].copy()
    return b, m


def _resolved_electric_meter_map(meters: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    elec = meters[meters["utility"].astype(str).str.lower().eq("electricity")].copy()
    elec = elec[~elec["status"].astype(str).str.lower().eq("unmetered")].copy()
    if elec.empty:
        return elec, elec

    counts = elec.groupby("meter_id")["building_id"].transform("size")
    elec["mapping_count"] = counts

    unique_mask = counts.eq(1)
    elec.loc[unique_mask & elec["allocation_pct"].isna(), "allocation_pct"] = 1.0

    shared = elec[counts.gt(1)].copy()
    if not shared.empty:
        stats = shared.groupby("meter_id").agg(
            rows=("building_id", "size"),
            allocations_present=("allocation_pct", lambda x: int(x.notna().sum())),
            allocation_sum=("allocation_pct", "sum"),
        ).reset_index()
        bad_ids = stats.loc[
            (stats["rows"] != stats["allocations_present"]) | ~np.isclose(stats["allocation_sum"], 1.0, atol=0.001),
            "meter_id",
        ]
    else:
        bad_ids = pd.Series(dtype="string")

    issues = elec[elec["meter_id"].isin(set(bad_ids))].copy()
    resolved = elec[~elec["meter_id"].isin(set(bad_ids))].copy()
    return resolved, issues


def allocate_bills_to_buildings(
    bills: pd.DataFrame,
    buildings: pd.DataFrame,
    meters: pd.DataFrame,
    electric_accounts: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    b, m = prepare_references(buildings, meters)
    resolved, shared_issues = _resolved_electric_meter_map(m)

    source = bills.copy().reset_index(drop=True)
    source["_bill_row_id"] = np.arange(len(source))
    source["accountNumber"] = source["accountNumber"].map(normalize_account_number)

    meter_allocated = source.merge(
        resolved[["meter_id", "building_id", "rate_schedule", "allocation_pct", "status"]],
        how="inner", left_on="fullMeterID", right_on="meter_id",
    )
    meter_allocated["mappingMethod"] = "meter"
    mapped_row_ids = set(meter_allocated["_bill_row_id"].tolist())

    account_allocated = pd.DataFrame()
    account_map = electric_accounts.copy() if electric_accounts is not None else pd.DataFrame()
    if not account_map.empty:
        account_map["account_number"] = account_map["account_number"].map(normalize_account_number)
        account_map["building_id"] = account_map["building_id"].astype(str).str.strip()
        account_map["allocation_pct"] = pd.to_numeric(account_map.get("allocation_pct", 1.0), errors="coerce").fillna(1.0)
        remaining = source[~source["_bill_row_id"].isin(mapped_row_ids)].copy()
        account_allocated = remaining.merge(
            account_map[["account_number", "building_id", "allocation_pct"]],
            how="inner", left_on="accountNumber", right_on="account_number",
        )
        account_allocated["rate_schedule"] = account_allocated.get("rate", "")
        account_allocated["status"] = "Account mapped"
        account_allocated["meter_id"] = account_allocated["fullMeterID"]
        account_allocated["mappingMethod"] = "account"

    allocated = pd.concat([meter_allocated, account_allocated], ignore_index=True, sort=False)
    allocated = allocated.merge(
        b[["building_id", "property_id", "campus", "facility_name", "use_type", "area_sqft", "eui_category"]],
        how="left", on="building_id",
    )

    # If the building reference is missing/zero, use authoritative GSF from the account map.
    if not account_map.empty:
        gsf = account_map.groupby("building_id", as_index=False).agg(
            accountMappedGSF=("gsf", lambda s: s.sum(min_count=1)),
            mappedFacilityName=("account_name", lambda s: next((str(v).strip() for v in s if str(v).strip()), None)),
        )
        allocated = allocated.merge(gsf, how="left", on="building_id")
        allocated["area_sqft"] = allocated["accountMappedGSF"].where(allocated["accountMappedGSF"].gt(0), allocated["area_sqft"])
        allocated["facility_name"] = allocated["facility_name"].fillna(allocated["mappedFacilityName"])

    allocated["allocatedKWh"] = allocated["kWh"] * allocated["allocation_pct"]
    allocated["allocatedCost"] = allocated["totalBillAmount"] * allocated["allocation_pct"]
    allocated["allocatedNewCharges"] = allocated.get("newCharges", np.nan) * allocated["allocation_pct"]

    mapped_ids = set(allocated["_bill_row_id"].tolist())
    unmapped_rows = source[~source["_bill_row_id"].isin(mapped_ids)].copy()
    if not unmapped_rows.empty:
        unmapped = (unmapped_rows.groupby(["fullMeterID", "accountNumber", "accountName", "serviceAddress"], dropna=False)
            .agg(firstSeen=("dateBilled","min"), lastSeen=("dateBilled","max"), billCount=("dateBilled","count"), totalKWh=("kWh","sum"))
            .reset_index().sort_values(["lastSeen","fullMeterID"], ascending=[False,True]))
    else:
        unmapped = pd.DataFrame()
    return allocated, unmapped, shared_issues


def infer_meter_suggestions(
    bills: pd.DataFrame,
    meters: pd.DataFrame,
    buildings: pd.DataFrame,
    unmapped: pd.DataFrame,
) -> pd.DataFrame:
    if unmapped.empty:
        return pd.DataFrame(columns=["meter_id", "suggested_building_id", "accountNumber", "confidence", "reason"])

    active = meters.copy()
    active["meter_id"] = active["meter_id"].map(normalize_meter_id)
    active = active[
        active["utility"].astype(str).str.lower().eq("electricity")
        & ~active["status"].astype(str).str.lower().eq("unmetered")
    ]

    meter_buildings = active.groupby("meter_id")["building_id"].agg(lambda s: sorted(set(s))).to_dict()
    bill_map = bills[["accountNumber", "fullMeterID"]].drop_duplicates()
    bill_map["known_buildings"] = bill_map["fullMeterID"].map(meter_buildings)

    account_known: dict[str, set[str]] = {}
    for row in bill_map.itertuples(index=False):
        if isinstance(row.known_buildings, list):
            account_known.setdefault(row.accountNumber, set()).update(row.known_buildings)

    def address_candidates(address: object) -> list[str]:
        text = str(address or "").upper()
        matches = []
        for building_id in buildings["building_id"].dropna().astype(str):
            token = re.sub(r"^BUILDING\s*", "", building_id.upper()).strip()
            parts = [p for p in re.split(r"[/\s]+", token) if p]
            if not parts:
                continue
            if any(re.search(rf"(?<![A-Z0-9]){re.escape(part)}(?![A-Z0-9])", text) for part in parts):
                matches.append(building_id)
        return sorted(set(matches))

    suggestions = []
    for row in unmapped.itertuples(index=False):
        candidates = sorted(account_known.get(row.accountNumber, set()))
        if len(candidates) == 1:
            building_id = candidates[0]
            confidence = "High"
            reason = "The same FPL account has mapped meter history for only one building."
        else:
            address_matches = address_candidates(row.serviceAddress)
            if len(address_matches) == 1:
                building_id = address_matches[0]
                confidence = "Medium"
                reason = "The FPL service address uniquely matches a known Building ID; confirm before saving."
            else:
                building_id = None
                confidence = "Review"
                reason = "No single-building mapping can be inferred from existing account history or service address."

        suggestions.append({
            "meter_id": row.fullMeterID,
            "suggested_building_id": building_id,
            "accountNumber": row.accountNumber,
            "accountName": row.accountName,
            "serviceAddress": row.serviceAddress,
            "firstSeen": row.firstSeen,
            "lastSeen": row.lastSeen,
            "confidence": confidence,
            "reason": reason,
        })
    order = pd.CategoricalDtype(["High", "Medium", "Review"], ordered=True)
    result = pd.DataFrame(suggestions)
    result["confidence"] = result["confidence"].astype(order)
    return result.sort_values(["confidence", "lastSeen"], ascending=[True, False]).reset_index(drop=True)


def build_building_monthly(allocated: pd.DataFrame) -> pd.DataFrame:
    if allocated.empty:
        return pd.DataFrame()

    monthly = (
        allocated.groupby(
            [
                "building_id",
                "property_id",
                "campus",
                "facility_name",
                "use_type",
                "area_sqft",
                "eui_category",
                "billMonth",
            ],
            dropna=False,
        )
        .agg(
            kWh=("allocatedKWh", "sum"),
            cost=("allocatedCost", "sum"),
            newCharges=("allocatedNewCharges", "sum"),
            meterCount=("fullMeterID", "nunique"),
            accountCount=("accountNumber", "nunique"),
            rateScheduleCount=("rate_schedule", "nunique"),
        )
        .reset_index()
    )

    monthly["kWhPerSF"] = monthly["kWh"] / monthly["area_sqft"].where(monthly["area_sqft"] > 0)
    monthly["costPerSF"] = monthly["cost"] / monthly["area_sqft"].where(monthly["area_sqft"] > 0)
    monthly["electricEUI"] = monthly["kWh"] * KBTU_PER_KWH / monthly["area_sqft"].where(monthly["area_sqft"] > 0)
    monthly["effectiveCostPerKWh"] = monthly["cost"] / monthly["kWh"].where(monthly["kWh"] > 0)

    monthly = monthly.sort_values(["building_id", "billMonth"]).reset_index(drop=True)
    g = monthly.groupby("building_id", group_keys=False)
    monthly["previousCost"] = g["cost"].shift(1)
    monthly["previousKWh"] = g["kWh"].shift(1)
    monthly["previousEUI"] = g["electricEUI"].shift(1)
    monthly["costChangePct"] = (monthly["cost"] - monthly["previousCost"]) / monthly["previousCost"].replace(0, np.nan)
    monthly["kWhChangePct"] = (monthly["kWh"] - monthly["previousKWh"]) / monthly["previousKWh"].replace(0, np.nan)
    monthly["euiChangePct"] = (monthly["electricEUI"] - monthly["previousEUI"]) / monthly["previousEUI"].replace(0, np.nan)

    monthly["rolling12KWh"] = g["kWh"].rolling(12, min_periods=1).sum().reset_index(level=0, drop=True)
    monthly["rolling12Cost"] = g["cost"].rolling(12, min_periods=1).sum().reset_index(level=0, drop=True)
    monthly["rolling12Months"] = g.cumcount().add(1).clip(upper=12)
    monthly["rolling12ElectricEUI"] = monthly["rolling12KWh"] * KBTU_PER_KWH / monthly["area_sqft"].where(monthly["area_sqft"] > 0)
    monthly["rolling12CostPerSF"] = monthly["rolling12Cost"] / monthly["area_sqft"].where(monthly["area_sqft"] > 0)
    monthly["rolling12KWhPerSF"] = monthly["rolling12KWh"] / monthly["area_sqft"].where(monthly["area_sqft"] > 0)
    monthly["rolling12Complete"] = monthly["rolling12Months"].ge(12)

    return monthly


def build_snapshot(building_monthly: pd.DataFrame) -> pd.DataFrame:
    if building_monthly.empty:
        return pd.DataFrame()
    snapshot = (
        building_monthly.sort_values(["building_id", "billMonth"])
        .groupby("building_id", as_index=False)
        .tail(1)
        .copy()
    )
    snapshot["peerRank"] = np.nan
    peer_mask = (
        snapshot["eui_category"].eq("Normal Building")
        & snapshot["rolling12Complete"]
        & snapshot["rolling12ElectricEUI"].notna()
    )
    snapshot.loc[peer_mask, "peerRank"] = snapshot.loc[peer_mask, "rolling12ElectricEUI"].rank(method="min", ascending=False)
    return snapshot.sort_values("rolling12ElectricEUI", ascending=False, na_position="last").reset_index(drop=True)


def build_campus_monthly(building_monthly: pd.DataFrame) -> pd.DataFrame:
    if building_monthly.empty:
        return pd.DataFrame()

    rows = []
    for month, grp in building_monthly.groupby("billMonth"):
        eligible = grp[grp["area_sqft"].gt(0)].copy()
        represented_area = eligible["area_sqft"].sum()
        eligible_kwh = eligible["kWh"].sum()
        rows.append({
            "billMonth": month,
            "cost": grp["cost"].sum(),
            "kWh": grp["kWh"].sum(),
            "euiEligibleKWh": eligible_kwh,
            "buildingCount": grp["building_id"].nunique(),
            "representedAreaSF": represented_area,
            "electricEUI": eligible_kwh * KBTU_PER_KWH / represented_area if represented_area else np.nan,
            "effectiveCostPerKWh": grp["cost"].sum() / grp["kWh"].sum() if grp["kWh"].sum() else np.nan,
        })
    campus = pd.DataFrame(rows).sort_values("billMonth").reset_index(drop=True)
    campus["costChangePct"] = campus["cost"].pct_change(fill_method=None)
    campus["kWhChangePct"] = campus["kWh"].pct_change(fill_method=None)
    campus["rolling12Cost"] = campus["cost"].rolling(12, min_periods=1).sum()
    campus["rolling12KWh"] = campus["kWh"].rolling(12, min_periods=1).sum()
    campus["rolling12EligibleKWh"] = campus["euiEligibleKWh"].rolling(12, min_periods=1).sum()
    campus["rolling12Months"] = np.minimum(np.arange(1, len(campus) + 1), 12)
    campus["rolling12ElectricEUI"] = (
        campus["rolling12EligibleKWh"] * KBTU_PER_KWH
        / campus["representedAreaSF"].where(campus["representedAreaSF"] > 0)
    )
    campus["rolling12Complete"] = campus["rolling12Months"].ge(12)
    return campus



def allocate_demand_to_buildings(
    demand: pd.DataFrame,
    buildings: pd.DataFrame,
    meters: pd.DataFrame,
    electric_accounts: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if demand is None or demand.empty:
        return pd.DataFrame(), pd.DataFrame()

    b, m = prepare_references(buildings, meters)
    resolved, shared_issues = _resolved_electric_meter_map(m)
    d = demand.copy().reset_index(drop=True)
    d["_demand_row_id"] = np.arange(len(d))
    d["fullMeterID"] = d["fullMeterID"].map(normalize_meter_id)
    d["accountNumber"] = d["accountNumber"].map(normalize_account_number)
    d["dateBilled"] = pd.to_datetime(d["dateBilled"], errors="coerce")
    d["billMonth"] = d["dateBilled"].dt.to_period("M").dt.to_timestamp()

    ref_cols = ["meter_id", "building_id", "rate_schedule", "allocation_pct", "status"]
    meter_allocated = d.merge(resolved[ref_cols], how="inner", left_on="fullMeterID", right_on="meter_id")
    meter_allocated["mappingMethod"] = "meter"
    mapped_ids = set(meter_allocated["_demand_row_id"].tolist())

    account_allocated = pd.DataFrame()
    account_map = electric_accounts.copy() if electric_accounts is not None else pd.DataFrame()
    if not account_map.empty:
        account_map["account_number"] = account_map["account_number"].map(normalize_account_number)
        account_map["building_id"] = account_map["building_id"].astype(str).str.strip()
        account_map["allocation_pct"] = pd.to_numeric(account_map.get("allocation_pct", 1.0), errors="coerce").fillna(1.0)
        remaining = d[~d["_demand_row_id"].isin(mapped_ids)].copy()
        account_allocated = remaining.merge(account_map[["account_number","building_id","allocation_pct"]], how="inner", left_on="accountNumber", right_on="account_number")
        account_allocated["meter_id"] = account_allocated["fullMeterID"]
        account_allocated["rate_schedule"] = account_allocated.get("rateScheduleBill", "")
        account_allocated["status"] = "Account mapped"
        account_allocated["mappingMethod"] = "account"

    allocated = pd.concat([meter_allocated, account_allocated], ignore_index=True, sort=False)
    if allocated.empty:
        return pd.DataFrame(), d.copy()
    allocated = allocated.rename(columns={"rate_schedule":"referenceRateSchedule"})
    allocated = allocated.merge(
        b[["building_id", "property_id", "campus", "facility_name", "use_type", "area_sqft", "eui_category"]],
        how="left", on="building_id",
    )
    if not account_map.empty:
        gsf = account_map.groupby("building_id", as_index=False).agg(
            accountMappedGSF=("gsf", lambda s: s.sum(min_count=1)),
            mappedFacilityName=("account_name", lambda s: next((str(v).strip() for v in s if str(v).strip()), None)),
        )
        allocated = allocated.merge(gsf, how="left", on="building_id")
        allocated["area_sqft"] = allocated["accountMappedGSF"].where(allocated["accountMappedGSF"].gt(0), allocated["area_sqft"])
        allocated["facility_name"] = allocated["facility_name"].fillna(allocated["mappedFacilityName"])

    for field in ["genericDemandKW","onPeakDemandKW","maximumDemandKW","billedPeakDemandKW","demandChargeAmount"]:
        if field not in allocated.columns:
            allocated[field] = np.nan
        allocated[field] = pd.to_numeric(allocated[field], errors="coerce")
        allocated[f"allocated{field[0].upper()}{field[1:]}"] = allocated[field] * allocated["allocation_pct"]

    all_mapped = set(allocated["_demand_row_id"].tolist())
    used = d[~d["_demand_row_id"].isin(all_mapped)].copy()
    if not used.empty:
        unmapped = (used.groupby(["fullMeterID","accountNumber","accountName","serviceAddress"], dropna=False)
            .agg(firstSeen=("dateBilled","min"), lastSeen=("dateBilled","max"), demandRecordCount=("dateBilled","count"), latestPeakKW=("billedPeakDemandKW","last"))
            .reset_index().sort_values(["lastSeen","fullMeterID"], ascending=[False,True]))
    else:
        unmapped = pd.DataFrame()
    return allocated, unmapped


def build_building_demand_monthly(allocated_demand: pd.DataFrame) -> pd.DataFrame:
    """Aggregate meter-level billed peaks to building/month.

    For multi-meter buildings, summed kW is the sum of individual billed meter peaks.
    It is not a coincident campus/building interval peak unless those meters peak at
    the same time. The dashboard labels this explicitly.
    """
    if allocated_demand is None or allocated_demand.empty:
        return pd.DataFrame()

    group_cols = [
        "building_id",
        "property_id",
        "campus",
        "facility_name",
        "use_type",
        "area_sqft",
        "eui_category",
        "billMonth",
    ]
    monthly = (
        allocated_demand.groupby(group_cols, dropna=False)
        .agg(
            billedPeakDemandKW=("allocatedBilledPeakDemandKW", lambda x: x.sum(min_count=1)),
            genericDemandKW=("allocatedGenericDemandKW", lambda x: x.sum(min_count=1)),
            onPeakDemandKW=("allocatedOnPeakDemandKW", lambda x: x.sum(min_count=1)),
            maximumDemandKW=("allocatedMaximumDemandKW", lambda x: x.sum(min_count=1)),
            demandChargeAmount=("allocatedDemandChargeAmount", lambda x: x.sum(min_count=1)),
            meterCount=("fullMeterID", "nunique"),
            accountCount=("accountNumber", "nunique"),
            estimatedChargeRows=("demandChargeIsEstimated", "sum"),
        )
        .reset_index()
    )
    monthly["demandIntensityWPerSF"] = (
        monthly["billedPeakDemandKW"] * 1000
        / monthly["area_sqft"].where(monthly["area_sqft"] > 0)
    )
    monthly["demandChargePerSF"] = (
        monthly["demandChargeAmount"]
        / monthly["area_sqft"].where(monthly["area_sqft"] > 0)
    )

    monthly = monthly.sort_values(["building_id", "billMonth"]).reset_index(drop=True)
    g = monthly.groupby("building_id", group_keys=False)
    monthly["previousPeakDemandKW"] = g["billedPeakDemandKW"].shift(1)
    monthly["previousDemandIntensityWPerSF"] = g["demandIntensityWPerSF"].shift(1)
    monthly["demandChangePct"] = (
        monthly["billedPeakDemandKW"] - monthly["previousPeakDemandKW"]
    ) / monthly["previousPeakDemandKW"].replace(0, np.nan)
    monthly["demandIntensityChangePct"] = (
        monthly["demandIntensityWPerSF"] - monthly["previousDemandIntensityWPerSF"]
    ) / monthly["previousDemandIntensityWPerSF"].replace(0, np.nan)
    monthly["rolling12PeakDemandKW"] = (
        g["billedPeakDemandKW"].rolling(12, min_periods=1).max().reset_index(level=0, drop=True)
    )
    monthly["rolling12DemandChargeAmount"] = (
        g["demandChargeAmount"].rolling(12, min_periods=1).sum().reset_index(level=0, drop=True)
    )
    return monthly


def build_demand_snapshot(building_demand_monthly: pd.DataFrame) -> pd.DataFrame:
    if building_demand_monthly is None or building_demand_monthly.empty:
        return pd.DataFrame()
    snapshot = (
        building_demand_monthly.sort_values(["building_id", "billMonth"])
        .groupby("building_id", as_index=False)
        .tail(1)
        .copy()
    )
    snapshot["demandIntensityRank"] = np.nan
    eligible = snapshot["demandIntensityWPerSF"].notna()
    snapshot.loc[eligible, "demandIntensityRank"] = snapshot.loc[eligible, "demandIntensityWPerSF"].rank(
        method="min", ascending=False
    )
    return snapshot.sort_values("billedPeakDemandKW", ascending=False, na_position="last").reset_index(drop=True)


def run_analytics(
    bills: pd.DataFrame,
    buildings: pd.DataFrame,
    meters: pd.DataFrame,
    demand: pd.DataFrame | None = None,
    electric_accounts: pd.DataFrame | None = None,
) -> AnalyticsBundle:
    b, m = prepare_references(buildings, meters)
    allocated, unmapped, shared = allocate_bills_to_buildings(bills, b, m, electric_accounts)
    monthly = build_building_monthly(allocated)
    snapshot = build_snapshot(monthly)
    campus = build_campus_monthly(monthly)
    suggestions = infer_meter_suggestions(bills, m, b, unmapped)
    missing_area = b[b["area_sqft"].isna() | b["area_sqft"].le(0)].copy()
    dup_subset = [c for c in ["accountNumber", "dateBilled", "fullMeterID"] if c in bills.columns]
    duplicates = bills[bills.duplicated(subset=dup_subset, keep=False)].copy() if dup_subset else pd.DataFrame()

    demand_details = demand.copy() if demand is not None else pd.DataFrame()
    allocated_demand, unmapped_demand = allocate_demand_to_buildings(demand_details, b, m, electric_accounts)
    demand_monthly = build_building_demand_monthly(allocated_demand)
    demand_snapshot = build_demand_snapshot(demand_monthly)

    return AnalyticsBundle(
        bills=bills,
        allocated_bills=allocated,
        building_monthly=monthly,
        building_snapshot=snapshot,
        campus_monthly=campus,
        unmapped_meters=unmapped,
        shared_meter_issues=shared,
        meter_suggestions=suggestions,
        missing_area=missing_area,
        duplicate_bills=duplicates,
        demand_details=demand_details,
        allocated_demand=allocated_demand,
        building_demand_monthly=demand_monthly,
        building_demand_snapshot=demand_snapshot,
        unmapped_demand_meters=unmapped_demand,
    )

def build_exception_table(bundle: AnalyticsBundle, cost_threshold: float, usage_threshold: float, eui_threshold: float) -> pd.DataFrame:
    df = bundle.building_monthly.copy()
    if df.empty:
        return pd.DataFrame()
    latest = df.sort_values(["building_id", "billMonth"]).groupby("building_id", as_index=False).tail(1).copy()
    reasons = []
    for row in latest.itertuples(index=False):
        flags = []
        if pd.notna(row.costChangePct) and row.costChangePct > cost_threshold:
            flags.append(f"Cost +{row.costChangePct:.1%}")
        if pd.notna(row.kWhChangePct) and row.kWhChangePct > usage_threshold:
            flags.append(f"Usage +{row.kWhChangePct:.1%}")
        if pd.notna(row.euiChangePct) and row.euiChangePct > eui_threshold:
            flags.append(f"EUI +{row.euiChangePct:.1%}")
        if pd.notna(row.costChangePct) and pd.notna(row.kWhChangePct) and row.costChangePct > 0 and row.kWhChangePct < 0:
            flags.append("Cost up while usage down")
        if flags:
            reasons.append({
                "building_id": row.building_id,
                "facility_name": row.facility_name,
                "billMonth": row.billMonth,
                "cost": row.cost,
                "kWh": row.kWh,
                "electricEUI": row.electricEUI,
                "costChangePct": row.costChangePct,
                "kWhChangePct": row.kWhChangePct,
                "euiChangePct": row.euiChangePct,
                "reason": "; ".join(flags),
            })
    return pd.DataFrame(reasons)
