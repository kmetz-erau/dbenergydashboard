import pandas as pd
import pytest

from src.analytics import KBTU_PER_KWH, run_analytics
from src.loader import normalize_bill_history


def sample_bills():
    return normalize_bill_history(pd.DataFrame({
        "accountNumber": ["1", "1"],
        "accountName": ["B1", "B1"],
        "serviceAddress": ["B1", "B1"],
        "meterNumber": ["100", "100"],
        "billRaw.meterSymbol": ["MV", "MV"],
        "dateBilled": ["2026-01-15", "2026-02-15"],
        "periodStart": ["2025-12-15", "2026-01-15"],
        "periodEnd": ["2026-01-15", "2026-02-15"],
        "daysUsed": [31, 31],
        "kWh": [1000, 1200],
        "newCharges": [100, 130],
        "totalBillAmount": [100, 130],
    }))


def refs(allocation=1.0):
    buildings = pd.DataFrame([{
        "id": 1,
        "building_id": "Building 1",
        "property_id": "P1",
        "campus": "DB",
        "facility_name": "Building 1",
        "use_type": "Office",
        "area_sqft": 10000,
        "eui_category": "Normal Building",
        "active": True,
        "notes": None,
    }])
    meters = pd.DataFrame([{
        "id": 1,
        "campus": "DB",
        "meter_id": "MV100",
        "building_id": "Building 1",
        "utility": "Electricity",
        "rate_schedule": "GSD-1",
        "allocation_pct": allocation,
        "status": "Current",
        "active": True,
        "notes": None,
    }])
    return buildings, meters


def test_building_eui_and_variance():
    b, m = refs()
    bundle = run_analytics(sample_bills(), b, m)
    feb = bundle.building_monthly.iloc[-1]
    assert feb["electricEUI"] == pytest.approx(1200 * KBTU_PER_KWH / 10000)
    assert feb["kWhChangePct"] == pytest.approx(0.2)
    assert feb["costChangePct"] == pytest.approx(0.3)
    assert bundle.unmapped_meters.empty


def test_unmapped_meter_is_reported():
    b, m = refs()
    m["meter_id"] = "OTHER"
    bundle = run_analytics(sample_bills(), b, m)
    assert len(bundle.unmapped_meters) == 1
    assert bundle.unmapped_meters.iloc[0]["fullMeterID"] == "MV100"
