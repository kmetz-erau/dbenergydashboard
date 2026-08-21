from __future__ import annotations

from io import BytesIO

import pandas as pd
import pytest

from src.gas_analytics import build_combined_site_snapshot, run_gas_analytics
from src.gas_loader import load_gas_file, normalize_gas_upload
from src.storage import (
    get_engine,
    init_database,
    load_gas_accounts,
    load_gas_history,
    upsert_gas_history,
)


def _gas_refs():
    return pd.DataFrame([
        {
            "provider": "TECO",
            "utility_account": "221009610710",
            "erau_account": "22185-7460",
            "building_id": "Building 155",
            "account_name": "DORM #1, BLDG #155",
            "gas_meter_id": "SN008777",
            "rate_schedule": "General Service 2 (GS2)",
            "usage_group": "GAS-B155-1",
            "cost_component": "Bundled / Utility",
            "end_use": "Building",
            "usage_authoritative": True,
            "include_in_site_eui": True,
            "active": True,
        },
        {
            "provider": "GAS SOUTH",
            "utility_account": "5400595608",
            "erau_account": "22185-7460",
            "building_id": "Building 221",
            "account_name": "APOLLO HALL",
            "gas_meter_id": "ANX13788",
            "rate_schedule": None,
            "usage_group": "GAS-B221-1",
            "cost_component": "Supplier / Commodity",
            "end_use": "Building",
            "usage_authoritative": False,
            "include_in_site_eui": True,
            "active": True,
        },
    ])


def test_gas_loader_only_requires_account_cost_and_therms():
    raw = pd.DataFrame({
        "Utility Account": [221009610710],
        "Cost": ["$1,923.17"],
        "Therms": ["1,212.60"],
    })
    out = normalize_gas_upload(
        raw,
        default_month="2026-07-01",
        gas_accounts=_gas_refs(),
    )
    row = out.iloc[0]
    assert row["utility_account"] == "221009610710"
    assert row["provider"] == "TECO"
    assert row["building_id"] == "Building 155"
    assert row["therms"] == pytest.approx(1212.6)
    assert row["cost"] == pytest.approx(1923.17)
    assert row["billMonth"] == pd.Timestamp("2026-07-01")


def test_gas_loader_reads_three_column_excel_and_resolves_provider():
    df = pd.DataFrame({
        "Utility Account": ["5400595608"],
        "Cost": [267.85],
        "Therms": [369.1],
    })
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Gas History", index=False)
    result = load_gas_file(
        buf.getvalue(),
        "gas.xlsx",
        default_month="2026-07-01",
        gas_accounts=_gas_refs(),
    )
    assert len(result.rows) == 1
    assert result.rows.iloc[0]["provider"] == "GAS SOUTH"
    assert result.rows.iloc[0]["building_id"] == "Building 221"


def test_gas_loader_rejects_unregistered_utility_account():
    raw = pd.DataFrame({
        "Utility Account": ["9999999999"],
        "Cost": [100.0],
        "Therms": [50.0],
    })
    with pytest.raises(ValueError, match="not registered"):
        normalize_gas_upload(
            raw,
            default_month="2026-07-01",
            gas_accounts=_gas_refs(),
        )


def test_transport_supplier_therms_are_not_double_counted_but_costs_are_summed():
    buildings = pd.DataFrame([{
        "building_id": "Building 221", "property_id": "1", "campus": "DB",
        "facility_name": "Apollo", "use_type": "Residence", "area_sqft": 10000,
        "eui_category": "Normal Building", "active": True,
    }])
    accounts = pd.DataFrame([
        {
            "provider": "TECO", "utility_account": "T1", "building_id": "Building 221",
            "usage_group": "GAS-B221-1", "cost_component": "Utility / Delivery",
            "end_use": "Building", "usage_authoritative": True,
            "include_in_site_eui": True, "active": True,
        },
        {
            "provider": "GAS SOUTH", "utility_account": "G1", "building_id": "Building 221",
            "usage_group": "GAS-B221-1", "cost_component": "Supplier / Commodity",
            "end_use": "Building", "usage_authoritative": False,
            "include_in_site_eui": True, "active": True,
        },
    ])
    history = pd.DataFrame([
        {"billMonth": pd.Timestamp("2026-07-01"), "provider": "TECO", "utility_account": "T1", "therms": 369.1, "cost": 362.90},
        {"billMonth": pd.Timestamp("2026-07-01"), "provider": "GAS SOUTH", "utility_account": "G1", "therms": 369.1, "cost": 267.85},
    ])
    bundle = run_gas_analytics(history, accounts, buildings)
    row = bundle.building_monthly.iloc[0]
    assert row["therms"] == pytest.approx(369.1)
    assert row["cost"] == pytest.approx(630.75)
    assert row["gasEUI"] == pytest.approx(369.1 * 100 / 10000)


def test_generator_gas_is_excluded_from_site_eui_by_reference_flag():
    buildings = pd.DataFrame([{
        "building_id": "Building 610", "property_id": "1", "campus": "DB",
        "facility_name": "Student Union", "use_type": "Student Union", "area_sqft": 10000,
        "eui_category": "Normal Building", "active": True,
    }])
    accounts = pd.DataFrame([
        {
            "provider": "TECO", "utility_account": "GEN", "building_id": "Building 610",
            "usage_group": "GEN", "cost_component": "Utility / Delivery", "end_use": "Generator",
            "usage_authoritative": True, "include_in_site_eui": False, "active": True,
        },
        {
            "provider": "TECO", "utility_account": "BLDG", "building_id": "Building 610",
            "usage_group": "BLDG", "cost_component": "Bundled / Utility", "end_use": "Building",
            "usage_authoritative": True, "include_in_site_eui": True, "active": True,
        },
    ])
    history = pd.DataFrame([
        {"billMonth": pd.Timestamp("2026-07-01"), "provider": "TECO", "utility_account": "GEN", "therms": 1800.0, "cost": 2400.0},
        {"billMonth": pd.Timestamp("2026-07-01"), "provider": "TECO", "utility_account": "BLDG", "therms": 20.0, "cost": 100.0},
    ])
    bundle = run_gas_analytics(history, accounts, buildings)
    row = bundle.building_monthly.iloc[0]
    assert row["therms"] == pytest.approx(1820.0)
    assert row["siteEuiTherms"] == pytest.approx(20.0)
    assert row["gasEUI"] == pytest.approx(0.2)


def test_gas_history_upsert_is_idempotent(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'gas.db'}")
    init_database(engine, seed=False)
    rows = pd.DataFrame([{
        "billMonth": pd.Timestamp("2026-07-01"), "provider": "TECO",
        "utility_account": "221009610710", "therms": 1212.6, "cost": 1923.17,
    }])
    first = upsert_gas_history(engine, rows, "gas.xlsx")
    second = upsert_gas_history(engine, rows, "gas.xlsx")
    history = load_gas_history(engine)
    assert first == {"inserted": 1, "updated": 0}
    assert second == {"inserted": 0, "updated": 1}
    assert len(history) == 1


def test_seeded_gas_accounts_include_teco_and_gas_south(tmp_path, monkeypatch):
    # Use project seed files but a temporary database URL.
    engine = get_engine(f"sqlite:///{tmp_path / 'seed.db'}")
    init_database(engine, seed=True)
    accounts = load_gas_accounts(engine)
    assert "TECO" in set(accounts["provider"])
    assert "GAS SOUTH" in set(accounts["provider"])
    assert ((accounts["building_id"] == "Building 610") & (accounts["end_use"] == "Generator")).any()


def test_combined_site_eui_adds_electric_and_gas_and_respects_missing_gas():
    electric = pd.DataFrame([
        {"building_id": "Building 1", "facility_name": "B1", "use_type": "Office", "area_sqft": 10000,
         "eui_category": "Normal Building", "rolling12ElectricEUI": 50.0, "rolling12Cost": 10000.0,
         "rolling12Complete": True},
        {"building_id": "Building 2", "facility_name": "B2", "use_type": "Office", "area_sqft": 10000,
         "eui_category": "Normal Building", "rolling12ElectricEUI": 40.0, "rolling12Cost": 9000.0,
         "rolling12Complete": True},
    ])
    gas = pd.DataFrame([{
        "building_id": "Building 1", "rolling12Therms": 1000.0, "rolling12SiteEuiTherms": 1000.0,
        "rolling12GasCost": 1200.0, "rolling12SiteGasCost": 1200.0, "rolling12GasEUI": 10.0,
        "rolling12GasCostPerSF": 0.12, "rolling12Months": 12, "rolling12Complete": True,
    }])
    accounts = pd.DataFrame([
        {"building_id": "Building 1", "active": True},
    ])
    out = build_combined_site_snapshot(electric, gas, accounts)
    b1 = out[out["building_id"] == "Building 1"].iloc[0]
    b2 = out[out["building_id"] == "Building 2"].iloc[0]
    assert b1["rolling12SiteEUI"] == pytest.approx(60.0)
    assert b2["rolling12SiteEUI"] == pytest.approx(40.0)


def test_gas_history_loader_accepts_multiple_months_without_sidebar_default():
    refs = _gas_refs()
    raw = pd.DataFrame({
        "Billing Month": ["2026-06-01", "2026-07-01"],
        "Utility Account": ["221009610710", "221009610710"],
        "Cost": [100.0, 120.0],
        "Therms": [50.0, 55.0],
    })
    out = normalize_gas_upload(raw, gas_accounts=refs)
    assert len(out) == 2
    assert out["billMonth"].dt.strftime("%Y-%m").tolist() == ["2026-06", "2026-07"]


def test_gas_history_requires_billing_month_when_no_default():
    raw = pd.DataFrame({
        "Utility Account": ["221009610710"],
        "Cost": [100.0],
        "Therms": [50.0],
    })
    with pytest.raises(ValueError, match="Billing Month"):
        normalize_gas_upload(raw, gas_accounts=_gas_refs())


def test_seed_migration_adds_historical_gas_accounts_without_overwriting_existing(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'gas_hist_seed.db'}")
    init_database(engine, seed=True)
    accounts = load_gas_accounts(engine, include_inactive=True)
    assert ((accounts["provider"] == "TECO") & (accounts["utility_account"] == "211009956130")).any()
    assert ((accounts["provider"] == "GAS SOUTH") & (accounts["utility_account"] == "4933635109")).any()
