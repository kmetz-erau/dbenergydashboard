from __future__ import annotations

from io import BytesIO

import pandas as pd
import pytest

from src.analytics import run_analytics
from src.loader import load_monthly_workbook, normalize_bill_history, normalize_demand_details
from src.storage import get_engine, init_database, load_demand_history, upsert_demand_history


def test_normalize_demand_details_parses_simple_and_tou_bills():
    df = pd.DataFrame([
        {
            "accountNumber": "1",
            "accountName": "A",
            "serviceAddress": "Building 1",
            "dateBilled": "2026-08-15",
            "meterNumberPage2": "KU30198",
            "rate": "GSD-1 GENERAL SERVICE DEMAND",
            "page2RawText": (
                "Demand: ($15.03 per KW) $1,082.16\n"
                "Demand KW 1.20 60.00 72\n"
            ),
            "kWh": 43260,
            "totalBillAmount": 4495.40,
        },
        {
            "accountNumber": "2",
            "accountName": "B",
            "serviceAddress": "Building 2",
            "dateBilled": "2026-08-15",
            "meterNumberPage2": "MV51029",
            "rate": "GSDT-1 GENERAL SERVICE DEMAND TIME OF USE",
            "page2RawText": (
                "Demand charge: $14.23 per KW\n"
                "Maximum $0.79 per KW\n"
                "On-peak demand 1.00 120.00 120\n"
                "Maximum demand 120\n"
            ),
            "kWh": 61080,
            "totalBillAmount": 6689.57,
        },
    ])
    out = normalize_demand_details(df, "Selected Enriched")
    simple = out[out["fullMeterID"].eq("KU30198")].iloc[0]
    assert simple["genericDemandKW"] == pytest.approx(72)
    assert simple["demandChargePerKW"] == pytest.approx(15.03)
    assert simple["demandChargeAmount"] == pytest.approx(1082.16)
    assert not bool(simple["demandChargeIsEstimated"])

    tou = out[out["fullMeterID"].eq("MV51029")].iloc[0]
    assert tou["maximumDemandKW"] == pytest.approx(120)
    assert tou["onPeakDemandKW"] == pytest.approx(120)
    assert tou["billedPeakDemandKW"] == pytest.approx(120)
    assert tou["demandChargeAmount"] == pytest.approx(120 * 14.23 + 120 * 0.79)
    assert bool(tou["demandChargeIsEstimated"])


def test_workbook_loads_selected_enriched_demand():
    bill_history = pd.DataFrame({
        "accountNumber": ["1"],
        "accountName": ["A"],
        "serviceAddress": ["Building 1"],
        "meterNumber": ["30198"],
        "billRaw.meterSymbol": ["KU"],
        "dateBilled": ["2026-08-15"],
        "periodStart": ["2026-07-15"],
        "periodEnd": ["2026-08-15"],
        "daysUsed": [31],
        "kWh": [43260],
        "newCharges": [4495.40],
        "totalBillAmount": [4495.40],
    })
    enriched = bill_history.copy()
    enriched["meterNumberPage2"] = "KU30198"
    enriched["rate"] = "GSD-1 GENERAL SERVICE DEMAND"
    enriched["page2RawText"] = "Demand: ($15.03 per KW) $1,082.16\nDemand KW 1.20 60.00 72\n"

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        bill_history.to_excel(writer, sheet_name="Bill History", index=False)
        enriched.to_excel(writer, sheet_name="Selected Enriched", index=False)
    result = load_monthly_workbook(buf.getvalue())
    assert len(result.demand) == 1
    assert result.demand.iloc[0]["billedPeakDemandKW"] == pytest.approx(72)


def test_demand_maps_to_building_and_calculates_intensity():
    bills = normalize_bill_history(pd.DataFrame({
        "accountNumber": ["1"],
        "accountName": ["A"],
        "serviceAddress": ["Building 1"],
        "meterNumber": ["30198"],
        "billRaw.meterSymbol": ["KU"],
        "dateBilled": ["2026-08-15"],
        "periodStart": ["2026-07-15"],
        "periodEnd": ["2026-08-15"],
        "daysUsed": [31],
        "kWh": [43260],
        "newCharges": [4495.40],
        "totalBillAmount": [4495.40],
    }))
    demand = pd.DataFrame([{
        "accountNumber": "1",
        "accountName": "A",
        "serviceAddress": "Building 1",
        "dateBilled": pd.Timestamp("2026-08-15"),
        "billMonth": pd.Timestamp("2026-08-01"),
        "fullMeterID": "KU30198",
        "rateScheduleBill": "GSD-1",
        "billRate": "GSD-1 GENERAL SERVICE DEMAND",
        "genericDemandKW": 72.0,
        "onPeakDemandKW": None,
        "maximumDemandKW": None,
        "billedPeakDemandKW": 72.0,
        "demandChargePerKW": 15.03,
        "maximumDemandChargePerKW": None,
        "demandChargeAmount": 1082.16,
        "demandChargeIsEstimated": False,
        "kWh": 43260,
        "totalBillAmount": 4495.40,
        "sourceSheet": "Selected Enriched",
    }])
    buildings = pd.DataFrame([{
        "id": 1, "building_id": "Building 1", "property_id": "P1", "campus": "DB",
        "facility_name": "B1", "use_type": "Office", "area_sqft": 10000,
        "eui_category": "Normal Building", "active": True, "notes": None,
    }])
    meters = pd.DataFrame([{
        "id": 1, "campus": "DB", "meter_id": "KU30198", "building_id": "Building 1",
        "utility": "Electricity", "rate_schedule": "GSD-1", "allocation_pct": 1.0,
        "status": "Current", "active": True, "notes": None,
    }])
    bundle = run_analytics(bills, buildings, meters, demand)
    row = bundle.building_demand_monthly.iloc[0]
    assert row["billedPeakDemandKW"] == pytest.approx(72)
    assert row["demandIntensityWPerSF"] == pytest.approx(7.2)
    assert row["demandChargeAmount"] == pytest.approx(1082.16)


def test_demand_history_upsert_is_idempotent(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'demand.db'}")
    init_database(engine, seed=False)
    demand = pd.DataFrame([{
        "accountNumber": "1", "accountName": "A", "serviceAddress": "B1",
        "dateBilled": pd.Timestamp("2026-08-15"), "billMonth": pd.Timestamp("2026-08-01"),
        "fullMeterID": "KU30198", "rateScheduleBill": "GSD-1", "billRate": "GSD-1",
        "genericDemandKW": 72.0, "onPeakDemandKW": None, "maximumDemandKW": None,
        "billedPeakDemandKW": 72.0, "demandChargePerKW": 15.03,
        "maximumDemandChargePerKW": None, "demandChargeAmount": 1082.16,
        "demandChargeIsEstimated": False, "kWh": 43260.0, "totalBillAmount": 4495.40,
        "sourceSheet": "Selected Enriched",
    }])
    first = upsert_demand_history(engine, demand, "a.xlsx")
    second = upsert_demand_history(engine, demand, "a.xlsx")
    history = load_demand_history(engine)
    assert first == {"inserted": 1, "updated": 0}
    assert second == {"inserted": 0, "updated": 1}
    assert len(history) == 1


def test_demand_history_auto_migrates_legacy_database(tmp_path):
    """An existing pre-demand DB is upgraded without deleting reference data."""
    from sqlalchemy import insert, select
    from src.storage import (
        buildings_table,
        demand_history_table,
        get_engine,
        metadata,
        upsert_demand_history,
    )

    db_path = tmp_path / "legacy.db"
    engine = get_engine(f"sqlite:///{db_path}")

    # Simulate an older deployment that had buildings but no demand_history.
    buildings_table.create(engine, checkfirst=True)
    with engine.begin() as conn:
        conn.execute(insert(buildings_table).values(
            building_id="Building TEST",
            campus="DB",
            area_sqft=10000,
            eui_category="Normal Building",
            active=True,
        ))

    demand = pd.DataFrame([{
        "accountNumber": "123456",
        "accountName": "Test",
        "serviceAddress": "TEST",
        "dateBilled": pd.Timestamp("2026-08-01"),
        "fullMeterID": "MV12345",
        "billedPeakDemandKW": 100.0,
        "demandChargeIsEstimated": False,
        "kWh": 1000.0,
        "totalBillAmount": 500.0,
    }])

    result = upsert_demand_history(engine, demand, "legacy-upgrade.xlsx")
    assert result["inserted"] == 1

    # New table exists and the old building row remains intact.
    with engine.connect() as conn:
        assert conn.execute(select(demand_history_table.c.id)).first() is not None
        assert conn.execute(select(buildings_table.c.building_id)).scalar_one() == "Building TEST"
