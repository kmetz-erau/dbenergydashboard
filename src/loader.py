from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import BinaryIO

import numpy as np
import pandas as pd

REQUIRED_BILL_COLUMNS = {
    "accountNumber",
    "dateBilled",
    "meterNumber",
    "kWh",
    "totalBillAmount",
}

DATE_COLUMNS = [
    "dateBilled",
    "periodStart",
    "periodEnd",
    "billRaw.dueDate",
    "billRaw.meterReadDate",
]

NUMERIC_COLUMNS = [
    "daysUsed",
    "kWh",
    "newCharges",
    "totalBillAmount",
    "billRaw.budgetBillAmount",
    "billRaw.creditAmount",
    "billRaw.daysBilled",
    "billRaw.daysUsed",
    "billRaw.lastBillAmount",
    "billRaw.newCharges",
    "billRaw.pastDueAmount",
    "billRaw.serviceAppChrg",
    "billRaw.totalBillAmount",
    "billRaw.totPymtRcvd",
]


@dataclass
class WorkbookLoadResult:
    bills: pd.DataFrame
    sheet_names: list[str]
    source_name: str


def normalize_meter_id(value: object) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    text = str(value).strip().upper()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return re.sub(r"[^A-Z0-9]", "", text)


def clean_account(value: object) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _read_bytes(source: bytes | BinaryIO) -> bytes:
    if isinstance(source, bytes):
        return source
    source.seek(0)
    return source.read()


def load_monthly_workbook(source: bytes | BinaryIO, source_name: str = "uploaded.xlsx") -> WorkbookLoadResult:
    raw = _read_bytes(source)
    excel = pd.ExcelFile(io.BytesIO(raw), engine="openpyxl")
    if "Bill History" not in excel.sheet_names:
        raise ValueError("Workbook must contain a 'Bill History' sheet.")

    bills = pd.read_excel(excel, sheet_name="Bill History")
    missing = sorted(REQUIRED_BILL_COLUMNS - set(bills.columns))
    if missing:
        raise ValueError(f"Bill History is missing required columns: {', '.join(missing)}")

    bills = normalize_bill_history(bills)
    return WorkbookLoadResult(bills=bills, sheet_names=excel.sheet_names, source_name=source_name)


def normalize_bill_history(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    out["accountNumber"] = out["accountNumber"].map(clean_account)
    out["meterNumber"] = out["meterNumber"].map(normalize_meter_id)

    meter_symbol_col = "billRaw.meterSymbol"
    if meter_symbol_col not in out.columns:
        out[meter_symbol_col] = ""
    out[meter_symbol_col] = out[meter_symbol_col].map(normalize_meter_id)
    out["fullMeterID"] = (
        out[meter_symbol_col].fillna("").astype(str)
        + out["meterNumber"].fillna("").astype(str)
    ).map(normalize_meter_id)

    for col in DATE_COLUMNS:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors="coerce")

    for col in NUMERIC_COLUMNS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    out["dateBilled"] = pd.to_datetime(out["dateBilled"], errors="coerce")
    out["billMonth"] = out["dateBilled"].dt.to_period("M").dt.to_timestamp()

    if "daysUsed" not in out.columns:
        out["daysUsed"] = np.nan
    if "periodStart" in out.columns and "periodEnd" in out.columns:
        calculated = (out["periodEnd"] - out["periodStart"]).dt.days
        out["daysUsed"] = out["daysUsed"].fillna(calculated)

    out["kWhPerDay"] = out["kWh"] / out["daysUsed"].where(out["daysUsed"] > 0)
    out["costPerDay"] = out["totalBillAmount"] / out["daysUsed"].where(out["daysUsed"] > 0)
    out["effectiveCostPerKWh"] = out["totalBillAmount"] / out["kWh"].where(out["kWh"] > 0)

    out = out.sort_values(["accountNumber", "dateBilled", "fullMeterID"], kind="stable").reset_index(drop=True)
    g = out.groupby("accountNumber", dropna=False)
    out["previousBillDate"] = g["dateBilled"].shift(1)
    out["previousBillAmount"] = g["totalBillAmount"].shift(1)
    out["previousKWh"] = g["kWh"].shift(1)
    out["previousDaysUsed"] = g["daysUsed"].shift(1)
    out["billAmountChange"] = out["totalBillAmount"] - out["previousBillAmount"]
    out["billAmountChangePct"] = out["billAmountChange"] / out["previousBillAmount"].replace(0, np.nan)
    out["kWhChange"] = out["kWh"] - out["previousKWh"]
    out["kWhChangePct"] = out["kWhChange"] / out["previousKWh"].replace(0, np.nan)

    previous_kwh_day = out["previousKWh"] / out["previousDaysUsed"].where(out["previousDaysUsed"] > 0)
    previous_cost_day = out["previousBillAmount"] / out["previousDaysUsed"].where(out["previousDaysUsed"] > 0)
    out["kWhPerDayChangePct"] = (out["kWhPerDay"] - previous_kwh_day) / previous_kwh_day.replace(0, np.nan)
    out["costPerDayChangePct"] = (out["costPerDay"] - previous_cost_day) / previous_cost_day.replace(0, np.nan)

    return out
