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

DEMAND_NUMERIC_COLUMNS = [
    "kWh",
    "totalBillAmount",
    "genericDemandKW",
    "onPeakDemandKW",
    "maximumDemandKW",
    "demandChargePerKW",
    "maximumDemandChargePerKW",
    "demandChargeAmount",
]

DEMAND_OUTPUT_COLUMNS = [
    "accountNumber",
    "accountName",
    "serviceAddress",
    "dateBilled",
    "billMonth",
    "fullMeterID",
    "rateScheduleBill",
    "billRate",
    "genericDemandKW",
    "onPeakDemandKW",
    "maximumDemandKW",
    "billedPeakDemandKW",
    "demandChargePerKW",
    "maximumDemandChargePerKW",
    "demandChargeAmount",
    "demandChargeIsEstimated",
    "kWh",
    "totalBillAmount",
    "sourceSheet",
]


@dataclass
class WorkbookLoadResult:
    bills: pd.DataFrame
    demand: pd.DataFrame
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


def _series(df: pd.DataFrame, name: str, default=np.nan) -> pd.Series:
    if name in df.columns:
        return df[name]
    return pd.Series(default, index=df.index)


def _text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value)


def _last_number_from_line(text: str, label_pattern: str) -> float:
    match = re.search(rf"(?im)^{label_pattern}\s+([^\n]+)", text)
    if not match:
        return np.nan
    values = re.findall(r"-?[0-9]+(?:\.[0-9]+)?", match.group(1))
    if not values:
        return np.nan
    try:
        return float(values[-1])
    except ValueError:
        return np.nan


def _regex_float(text: str, pattern: str, group: int = 1) -> float:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return np.nan
    try:
        return float(match.group(group).replace(",", ""))
    except (ValueError, AttributeError):
        return np.nan


def _demand_charge_from_text(text: str) -> tuple[float, float]:
    """Return (rate_per_kw, exact_charge_amount) for simple demand bills."""
    match = re.search(
        r"Demand:\s*\(\s*\$?([0-9.]+)\s*per\s*KW\s*\)\s*\$?([0-9,]+(?:\.[0-9]+)?)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return np.nan, np.nan
    return float(match.group(1)), float(match.group(2).replace(",", ""))


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
    demand = load_demand_details(excel)
    return WorkbookLoadResult(
        bills=bills,
        demand=demand,
        sheet_names=excel.sheet_names,
        source_name=source_name,
    )


def load_demand_details(excel: pd.ExcelFile) -> pd.DataFrame:
    """Load the richest demand-bearing sheet available in the enhanced workbook."""
    for sheet in ["Selected Enriched", "PDF Details"]:
        if sheet in excel.sheet_names:
            df = pd.read_excel(excel, sheet_name=sheet)
            return normalize_demand_details(df, source_sheet=sheet)
    return pd.DataFrame(columns=DEMAND_OUTPUT_COLUMNS)


def normalize_demand_details(df: pd.DataFrame, source_sheet: str = "Demand Details") -> pd.DataFrame:
    """Normalize structured + PDF-text demand fields into one demand history schema.

    The current collector fully structures TOU demand rows, while GSD-1 bills often
    leave `Demand: ($x.xx per KW) $amount` and `Demand KW ...` only in page-2 text.
    This function fills those gaps without requiring OCR.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=DEMAND_OUTPUT_COLUMNS)

    out = df.copy()
    for col in ["accountNumber", "accountName", "serviceAddress", "dateBilled", "kWh", "totalBillAmount", "rate", "page2RawText"]:
        if col not in out.columns:
            out[col] = np.nan

    out["accountNumber"] = out["accountNumber"].map(clean_account)
    out["dateBilled"] = pd.to_datetime(out["dateBilled"], errors="coerce")
    out["billMonth"] = out["dateBilled"].dt.to_period("M").dt.to_timestamp()
    out["kWh"] = pd.to_numeric(out["kWh"], errors="coerce")
    out["totalBillAmount"] = pd.to_numeric(out["totalBillAmount"], errors="coerce")

    # Prefer the full page-2 meter identifier (e.g. MV32681). Fall back to
    # billRaw.meterSymbol + meterNumber if needed.
    page2_meter = _series(out, "meterNumberPage2", "").map(normalize_meter_id)
    symbol = _series(out, "billRaw.meterSymbol", "").map(normalize_meter_id)
    meter_number = _series(out, "meterNumber", "").map(normalize_meter_id)
    reconstructed = (symbol.fillna("").astype(str) + meter_number.fillna("").astype(str)).map(normalize_meter_id)
    out["fullMeterID"] = page2_meter.where(page2_meter.ne(""), reconstructed)

    out["billRate"] = out["rate"].map(_text).str.strip()
    out["rateScheduleBill"] = out["billRate"].str.extract(r"^\s*([A-Z0-9-]+)", expand=False)

    raw_text = out["page2RawText"].map(_text)

    # Existing structured fields first.
    generic_existing = pd.to_numeric(_series(out, "demandKW"), errors="coerce")
    on_peak_existing = pd.to_numeric(_series(out, "onPeakDemandKW"), errors="coerce")
    maximum_existing = pd.to_numeric(_series(out, "maximumDemandKW"), errors="coerce")
    rate_existing = pd.to_numeric(_series(out, "demandChargePerKW"), errors="coerce")
    maximum_rate_existing = pd.to_numeric(_series(out, "maximumDemandChargePerKW"), errors="coerce")
    amount_existing = pd.to_numeric(_series(out, "demandChargeAmount"), errors="coerce")

    generic_parsed = raw_text.map(lambda t: _last_number_from_line(t, r"Demand\s+KW"))
    on_peak_parsed = raw_text.map(lambda t: _last_number_from_line(t, r"On-peak\s+demand"))
    maximum_parsed = raw_text.map(lambda t: _last_number_from_line(t, r"Maximum\s+demand"))

    out["genericDemandKW"] = generic_existing.fillna(generic_parsed)
    out["onPeakDemandKW"] = on_peak_existing.fillna(on_peak_parsed)
    out["maximumDemandKW"] = maximum_existing.fillna(maximum_parsed)

    simple_pairs = raw_text.map(_demand_charge_from_text)
    simple_rate = simple_pairs.map(lambda x: x[0])
    simple_amount = simple_pairs.map(lambda x: x[1])

    tou_rate = raw_text.map(
        lambda t: _regex_float(t, r"Demand charge:\s*\$?([0-9.]+)\s*per\s*KW")
    )
    maximum_rate = raw_text.map(
        lambda t: _regex_float(t, r"Maximum\s*\$?([0-9.]+)\s*per\s*KW")
    )

    out["demandChargePerKW"] = rate_existing.fillna(tou_rate).fillna(simple_rate)
    out["maximumDemandChargePerKW"] = maximum_rate_existing.fillna(maximum_rate)

    # Exact dollar demand charge exists on many GSD-1 bills. TOU bills expose
    # component rates, so calculate a transparent estimate from billed kW.
    exact_amount = amount_existing.fillna(simple_amount)
    simple_estimate = out["genericDemandKW"] * out["demandChargePerKW"]
    tou_estimate = (
        out["onPeakDemandKW"] * out["demandChargePerKW"]
        + out["maximumDemandKW"] * out["maximumDemandChargePerKW"]
    )
    estimate = tou_estimate.where(
        out["onPeakDemandKW"].notna()
        & out["demandChargePerKW"].notna()
        & out["maximumDemandKW"].notna()
        & out["maximumDemandChargePerKW"].notna(),
        simple_estimate,
    )
    out["demandChargeAmount"] = exact_amount.fillna(estimate)
    out["demandChargeIsEstimated"] = exact_amount.isna() & estimate.notna()

    # For a single headline peak metric, simple-demand bills use Demand KW;
    # TOU demand bills use Maximum demand. This is a billed meter peak, not a
    # coincident whole-campus peak.
    out["billedPeakDemandKW"] = out["genericDemandKW"].fillna(out["maximumDemandKW"]).fillna(out["onPeakDemandKW"])
    out["sourceSheet"] = source_sheet

    has_demand = out[
        ["genericDemandKW", "onPeakDemandKW", "maximumDemandKW", "billedPeakDemandKW", "demandChargeAmount"]
    ].notna().any(axis=1)
    out = out.loc[has_demand].copy()

    for col in DEMAND_NUMERIC_COLUMNS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    for col in DEMAND_OUTPUT_COLUMNS:
        if col not in out.columns:
            out[col] = np.nan

    return (
        out[DEMAND_OUTPUT_COLUMNS]
        .sort_values(["dateBilled", "accountNumber", "fullMeterID"], kind="stable")
        .reset_index(drop=True)
    )


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
