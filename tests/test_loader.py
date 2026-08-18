from io import BytesIO

import pandas as pd

from src.loader import load_monthly_workbook, normalize_bill_history


def test_meter_id_reconstruction_and_variance():
    df = pd.DataFrame({
        "accountNumber": [1001, 1001],
        "accountName": ["Test", "Test"],
        "serviceAddress": ["Building 1", "Building 1"],
        "meterNumber": [32681, 32681],
        "billRaw.meterSymbol": ["MV", "MV"],
        "dateBilled": ["2026-01-15", "2026-02-15"],
        "periodStart": ["2025-12-15", "2026-01-15"],
        "periodEnd": ["2026-01-15", "2026-02-15"],
        "daysUsed": [31, 31],
        "kWh": [1000, 1100],
        "newCharges": [100, 120],
        "totalBillAmount": [100, 120],
    })
    out = normalize_bill_history(df)
    assert out.loc[0, "fullMeterID"] == "MV32681"
    assert out.loc[1, "kWhChangePct"] == 0.1
    assert out.loc[1, "billAmountChangePct"] == 0.2


def test_load_monthly_workbook_requires_bill_history():
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame({"a": [1]}).to_excel(writer, sheet_name="Other", index=False)
    try:
        load_monthly_workbook(buf.getvalue())
        raise AssertionError("Expected ValueError")
    except ValueError as exc:
        assert "Bill History" in str(exc)
