from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from src.electric_accounts import normalize_account_number
from src.loader import DEMAND_OUTPUT_COLUMNS, normalize_bill_history, normalize_demand_details


@dataclass
class DuckDBLoadResult:
    electric_history: pd.DataFrame
    demand_history: pd.DataFrame
    gas_history: pd.DataFrame
    electric_accounts: pd.DataFrame
    tables: list[str]
    source_name: str


def _table_lookup(tables: list[str]) -> dict[str, str]:
    return {str(name).lower(): str(name) for name in tables}


def _pick_table(tables: list[str], *candidates: str) -> str | None:
    lookup = _table_lookup(tables)
    for candidate in candidates:
        if candidate.lower() in lookup:
            return lookup[candidate.lower()]
    return None


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _read_table(con: Any, table: str) -> pd.DataFrame:
    return con.execute(f"SELECT * FROM {_quote_identifier(table)}").df()


def _normalize_storage_electric(df: pd.DataFrame) -> pd.DataFrame:
    rename = {
        "account_number": "accountNumber",
        "account_name": "accountName",
        "service_address": "serviceAddress",
        "meter_number": "meterNumber",
        "full_meter_id": "fullMeterID",
        "date_billed": "dateBilled",
        "period_start": "periodStart",
        "period_end": "periodEnd",
        "days_used": "daysUsed",
        "kwh": "kWh",
        "new_charges": "newCharges",
        "total_bill_amount": "totalBillAmount",
        "source_file": "sourceFile",
    }
    out = df.rename(columns={k: v for k, v in rename.items() if k in df.columns}).copy()
    # normalize_bill_history builds fullMeterID if necessary and standardizes types.
    return normalize_bill_history(out)


def _normalize_duckdb_demand(df: pd.DataFrame, source_table: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=DEMAND_OUTPUT_COLUMNS)

    rename = {
        "account_number": "accountNumber",
        "account_name": "accountName",
        "service_address": "serviceAddress",
        "meter_id": "fullMeterID",
        "full_meter_id": "fullMeterID",
        "date_billed": "dateBilled",
        "rate_schedule_bill": "rateScheduleBill",
        "bill_rate": "billRate",
        "generic_demand_kw": "genericDemandKW",
        "on_peak_demand_kw": "onPeakDemandKW",
        "maximum_demand_kw": "maximumDemandKW",
        "billed_peak_demand_kw": "billedPeakDemandKW",
        "demand_charge_per_kw": "demandChargePerKW",
        "maximum_demand_charge_per_kw": "maximumDemandChargePerKW",
        "demand_charge_amount": "demandChargeAmount",
        "demand_charge_is_estimated": "demandChargeIsEstimated",
        "kwh": "kWh",
        "total_bill_amount": "totalBillAmount",
        "source_sheet": "sourceSheet",
        "source_file": "sourceFile",
    }
    out = df.rename(columns={k: v for k, v in rename.items() if k in df.columns}).copy()

    # Raw collector/enriched tables can be parsed using the existing demand parser.
    raw_markers = {"page2RawText", "rate", "meterNumberPage2", "maximumDemandChargePerKW"}
    if raw_markers.intersection(out.columns) and "billedPeakDemandKW" not in out.columns:
        return normalize_demand_details(out, source_sheet=source_table)

    for col in ["accountNumber", "accountName", "serviceAddress", "dateBilled", "fullMeterID"]:
        if col not in out.columns:
            out[col] = pd.NA
    out["accountNumber"] = out["accountNumber"].map(normalize_account_number)
    out["dateBilled"] = pd.to_datetime(out["dateBilled"], errors="coerce")
    out["billMonth"] = out["dateBilled"].dt.to_period("M").dt.to_timestamp()
    if "sourceSheet" not in out.columns:
        out["sourceSheet"] = source_table
    numeric = [
        "genericDemandKW", "onPeakDemandKW", "maximumDemandKW", "billedPeakDemandKW",
        "demandChargePerKW", "maximumDemandChargePerKW", "demandChargeAmount",
        "kWh", "totalBillAmount",
    ]
    for col in numeric:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    if "billedPeakDemandKW" not in out.columns:
        candidates = [c for c in ["genericDemandKW", "onPeakDemandKW", "maximumDemandKW"] if c in out.columns]
        out["billedPeakDemandKW"] = out[candidates].max(axis=1, skipna=True) if candidates else pd.NA
    if "demandChargeIsEstimated" not in out.columns:
        out["demandChargeIsEstimated"] = False
    for col in DEMAND_OUTPUT_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA
    return out[DEMAND_OUTPUT_COLUMNS]


def _normalize_gas(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    rename = {
        "bill_month": "billMonth",
        "utility_account": "utility_account",
        "account_number": "utility_account",
        "total_cost": "cost",
        "total_therms": "therms",
        "source_file": "source_file",
    }
    out = df.rename(columns={k: v for k, v in rename.items() if k in df.columns}).copy()
    if "billMonth" in out.columns:
        out["billMonth"] = pd.to_datetime(out["billMonth"], errors="coerce").dt.to_period("M").dt.to_timestamp()
    for col in ["cost", "therms"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _normalize_electric_accounts(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    rename = {
        "accountNumber": "account_number",
        "fpl_account": "account_number",
        "fplAccount": "account_number",
        "buildingNumber": "building_id",
        "building_number": "building_id",
        "bldg": "building_id",
        "GSF": "gsf",
        "area_sqft": "gsf",
        "Name": "account_name",
        "buildingName": "account_name",
        "building_name": "account_name",
    }
    out = df.rename(columns={k: v for k, v in rename.items() if k in df.columns}).copy()
    if "account_number" not in out.columns or "building_id" not in out.columns:
        return pd.DataFrame()
    out["account_number"] = out["account_number"].map(normalize_account_number)
    out["building_id"] = out["building_id"].astype(str).str.strip()
    # Match the dashboard's existing building identifiers.
    out["building_id"] = out["building_id"].map(
        lambda x: x if x.lower().startswith("building ") else f"Building {x}"
    )
    out["gsf"] = pd.to_numeric(out.get("gsf"), errors="coerce")
    if "allocation_pct" not in out.columns:
        out["allocation_pct"] = 1.0
    else:
        out["allocation_pct"] = pd.to_numeric(out["allocation_pct"], errors="coerce").fillna(1.0)
    if "active" in out.columns:
        active = out["active"].astype(str).str.lower().isin({"true", "1", "yes", "y"})
        out = out[active].copy()
    wanted = ["account_number", "building_id", "gsf", "account_name", "allocation_pct", "active", "notes"]
    return out[[c for c in wanted if c in out.columns]].drop_duplicates("account_number", keep="last")


def load_duckdb_bytes(raw: bytes, source_name: str = "energy.duckdb") -> DuckDBLoadResult:
    """Load dashboard-ready datasets from an uploaded DuckDB file.

    Supported table names (case-insensitive):
      electric: fpl_bills, electric_history
      demand:   fpl_demand, demand_history
      gas:      gas_history, gas_bills
      mapping:  electric_account_mapping, electric_accounts
    """
    try:
        import duckdb  # lazy import keeps normal Excel mode usable if package is absent
    except ImportError as exc:  # pragma: no cover - deployment dependency path
        raise RuntimeError(
            "DuckDB support is not installed. Add 'duckdb>=1.4,<2' to requirements.txt and redeploy."
        ) from exc

    if not raw:
        raise ValueError("The uploaded DuckDB file is empty.")

    temp_path = None
    con = None
    try:
        with tempfile.NamedTemporaryFile(prefix="erau_energy_", suffix=".duckdb", delete=False) as tmp:
            tmp.write(raw)
            temp_path = tmp.name

        con = duckdb.connect(temp_path, read_only=True)
        tables_df = con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name"
        ).df()
        tables = tables_df["table_name"].astype(str).tolist()
        if not tables:
            raise ValueError("No tables were found in the uploaded DuckDB database.")

        electric_table = _pick_table(tables, "fpl_bills", "electric_history")
        demand_table = _pick_table(tables, "fpl_demand", "demand_history")
        gas_table = _pick_table(tables, "gas_history", "gas_bills")
        mapping_table = _pick_table(tables, "electric_account_mapping", "electric_accounts")

        electric = pd.DataFrame()
        if electric_table:
            electric_raw = _read_table(con, electric_table)
            if electric_table.lower() == "electric_history" or "account_number" in electric_raw.columns:
                electric = _normalize_storage_electric(electric_raw)
            else:
                electric = normalize_bill_history(electric_raw)

        demand = pd.DataFrame(columns=DEMAND_OUTPUT_COLUMNS)
        if demand_table:
            demand = _normalize_duckdb_demand(_read_table(con, demand_table), demand_table)
        elif electric_table and not electric.empty:
            # If fpl_bills itself contains the rich PDF/demand fields, parse them.
            electric_raw = _read_table(con, electric_table)
            if {"page2RawText", "onPeakDemandKW", "maximumDemandKW"}.intersection(electric_raw.columns):
                demand = normalize_demand_details(electric_raw, source_sheet=electric_table)

        gas = _normalize_gas(_read_table(con, gas_table)) if gas_table else pd.DataFrame()
        mappings = _normalize_electric_accounts(_read_table(con, mapping_table)) if mapping_table else pd.DataFrame()

        if electric.empty and demand.empty and gas.empty:
            raise ValueError(
                "The database does not contain a supported energy table. Expected one of: "
                "fpl_bills, electric_history, fpl_demand, demand_history, gas_history, gas_bills."
            )

        return DuckDBLoadResult(
            electric_history=electric,
            demand_history=demand,
            gas_history=gas,
            electric_accounts=mappings,
            tables=tables,
            source_name=source_name,
        )
    finally:
        if con is not None:
            con.close()
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
