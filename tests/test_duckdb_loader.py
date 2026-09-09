import pandas as pd

from src.duckdb_loader import _normalize_electric_accounts, _normalize_storage_electric


def test_duckdb_mapping_normalizes_fpl_accounts_and_building_ids():
    raw = pd.DataFrame({
        "accountNumber": ["0204486419", "4916435573"],
        "buildingNumber": ["311", "1500"],
        "GSF": [26941, 263766],
        "Name": ["SIMULATION CENTER", "STUDENT VILLAGE"],
    })
    out = _normalize_electric_accounts(raw)
    assert out["account_number"].tolist() == ["0204486419", "4916435573"]
    assert out["building_id"].tolist() == ["Building 311", "Building 1500"]
    assert out["gsf"].tolist() == [26941, 263766]


def test_duckdb_storage_electric_converts_storage_schema():
    raw = pd.DataFrame({
        "account_number": ["0204486419"],
        "meter_number": ["32681"],
        "full_meter_id": ["MV32681"],
        "date_billed": ["2026-08-07"],
        "kwh": [64200],
        "total_bill_amount": [7560.10],
    })
    out = _normalize_storage_electric(raw)
    assert out.loc[0, "accountNumber"] == "0204486419"
    assert out.loc[0, "fullMeterID"] == "MV32681"
    assert out.loc[0, "kWh"] == 64200
