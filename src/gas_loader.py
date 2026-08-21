from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import BinaryIO, NamedTuple

import numpy as np
import pandas as pd


# Internal normalized schema. Monthly users only need to upload
# Utility Account, Cost, and Therms. The remaining fields are resolved from
# Reference Data -> Gas Accounts.
NORMALIZED_COLUMNS = [
    "billMonth",
    "provider",
    "utility_account",
    "erau_account",
    "account_name",
    "building_id",
    "therms",
    "cost",
    "gas_meter_id",
    "rate_schedule",
    "usage_group",
    "cost_component",
    "end_use",
    "notes",
]

COLUMN_ALIASES = {
    "billmonth": "billMonth",
    "bill month": "billMonth",
    "billing month": "billMonth",
    "month": "billMonth",
    "provider": "provider",
    "vendor": "provider",
    "gas provider": "provider",
    "utility account": "utility_account",
    "utility account number": "utility_account",
    "account number": "utility_account",
    "account #": "utility_account",
    "acct#": "utility_account",
    "teco account": "utility_account",
    "gas south account": "utility_account",
    "erau account": "erau_account",
    "erau acct#": "erau_account",
    "erau acct": "erau_account",
    "name": "account_name",
    "account name": "account_name",
    "building": "building_id",
    "building id": "building_id",
    "bldg #": "building_id",
    "bldg": "building_id",
    "therms": "therms",
    "therm": "therms",
    "usage": "therms",
    "cost": "cost",
    "amount": "cost",
    "gas meter": "gas_meter_id",
    "gas meter id": "gas_meter_id",
    "meter id": "gas_meter_id",
    "rate schedule": "rate_schedule",
    "rate": "rate_schedule",
    "usage group": "usage_group",
    "cost component": "cost_component",
    "end use": "end_use",
    "notes": "notes",
}


class GasLoadResult(NamedTuple):
    rows: pd.DataFrame
    source_name: str


def _clean_header(value: object) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip().lower()


def _clean_string(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text or None


def normalize_building_id(value: object) -> str | None:
    """Retained for backwards compatibility with older gas upload files."""
    text = _clean_string(value)
    if not text:
        return None
    text = " ".join(text.split())
    if text.lower().startswith("building"):
        compact = text.replace(" ", "")
        if compact.lower() in {"building1500", "building1511"}:
            return "Building1500" if compact.lower() == "building1500" else "Building1511"
        return text
    if text == "1500":
        return "Building1500"
    if text == "1511":
        return "Building1511"
    if text in {"312", "312/320", "320"}:
        return "Building 312/320"
    return f"Building {text}"


def _read_table(source: bytes | BinaryIO, source_name: str) -> pd.DataFrame:
    raw = source if isinstance(source, bytes) else source.read()
    suffix = Path(source_name).suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(BytesIO(raw), dtype=str)

    excel = pd.ExcelFile(BytesIO(raw), engine="openpyxl")
    preferred = ["Gas History", "Gas", "Natural Gas", "Gas Upload"]
    sheet = next((name for name in preferred if name in excel.sheet_names), excel.sheet_names[0])
    # Object dtype helps preserve long account numbers exactly as displayed.
    return pd.read_excel(excel, sheet_name=sheet, dtype=object)


def _active_gas_account_reference(gas_accounts: pd.DataFrame | None) -> pd.DataFrame:
    if gas_accounts is None or gas_accounts.empty:
        return pd.DataFrame()

    ref = gas_accounts.copy()
    if "active" in ref.columns:
        ref = ref[ref["active"].fillna(True).astype(bool)].copy()

    ref["utility_account"] = ref["utility_account"].map(_clean_string).fillna("")
    ref = ref[ref["utility_account"].ne("")].copy()
    return ref


def _apply_reference_data(out: pd.DataFrame, gas_accounts: pd.DataFrame | None) -> pd.DataFrame:
    """Resolve provider/building/meter metadata from Utility Account.

    Utility Account is the authoritative join key for monthly gas uploads. The
    provider and all building metadata live in Reference Data -> Gas Accounts,
    so users do not need to repeat them in each monthly file.
    """
    ref = _active_gas_account_reference(gas_accounts)

    # Backwards compatibility: old normalized files that still provide provider
    # can be loaded without a reference table. The Streamlit app always supplies
    # gas_accounts, so the normal monthly workflow uses the reference lookup.
    if ref.empty:
        if "provider" not in out.columns or out["provider"].map(_clean_string).isna().any():
            raise ValueError(
                "Gas account reference data is required when the upload contains only "
                "Utility Account, Cost, and Therms. Add the account under "
                "Reference Data > Gas Accounts and try again."
            )
        return out

    # A utility account must resolve to exactly one active reference record.
    counts = ref.groupby("utility_account", dropna=False).size()
    ambiguous = sorted(counts[counts > 1].index.astype(str).tolist())
    if ambiguous:
        preview = ", ".join(ambiguous[:10])
        more = "..." if len(ambiguous) > 10 else ""
        raise ValueError(
            "The Gas Accounts reference table has duplicate active Utility Account values: "
            f"{preview}{more}. Make each active Utility Account unique before uploading."
        )

    ref_cols = [
        "utility_account",
        "provider",
        "erau_account",
        "account_name",
        "building_id",
        "gas_meter_id",
        "rate_schedule",
        "usage_group",
        "cost_component",
        "end_use",
        "notes",
    ]
    ref = ref[[c for c in ref_cols if c in ref.columns]].copy()
    ref = ref.rename(columns={c: f"_ref_{c}" for c in ref.columns if c != "utility_account"})

    merged = out.merge(ref, on="utility_account", how="left", validate="many_to_one")

    missing = sorted(
        merged.loc[merged.get("_ref_provider").isna(), "utility_account"]
        .dropna()
        .astype(str)
        .unique()
        .tolist()
    )
    if missing:
        preview = ", ".join(missing[:12])
        more = f" (+{len(missing) - 12} more)" if len(missing) > 12 else ""
        raise ValueError(
            "These Utility Account values are not registered in Reference Data > Gas Accounts: "
            f"{preview}{more}. Add them there, then upload the file again."
        )

    # Reference data is authoritative for account metadata.
    for field in [
        "provider",
        "erau_account",
        "account_name",
        "building_id",
        "gas_meter_id",
        "rate_schedule",
        "usage_group",
        "cost_component",
        "end_use",
        "notes",
    ]:
        ref_col = f"_ref_{field}"
        if ref_col in merged.columns:
            merged[field] = merged[ref_col]

    merged = merged.drop(columns=[c for c in merged.columns if c.startswith("_ref_")])
    return merged


def normalize_gas_upload(
    df: pd.DataFrame,
    default_month=None,
    gas_accounts: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Normalize a monthly gas upload.

    Monthly upload requirements are intentionally minimal:
      * Utility Account
      * Cost
      * Therms

    The selected ``default_month`` supplies the bill month for rows that do not
    contain a legacy billMonth column. Provider/building/meter metadata are
    looked up from the Gas Accounts reference table using Utility Account.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=NORMALIZED_COLUMNS + ["lineCostPerTherm"])

    rename = {}
    for col in df.columns:
        clean = _clean_header(col)
        if clean in COLUMN_ALIASES:
            rename[col] = COLUMN_ALIASES[clean]
        elif col in NORMALIZED_COLUMNS:
            rename[col] = col
    out = df.rename(columns=rename).copy()

    required = {"utility_account", "therms", "cost"}
    missing = sorted(required - set(out.columns))
    if missing:
        raise ValueError(
            "Gas upload is missing required columns: " + ", ".join(missing)
            + ". The monthly gas file only needs Utility Account, Cost, and Therms."
        )

    # Keep only recognized columns so stray spreadsheet notes do not enter the
    # analytics pipeline.
    recognized = [c for c in out.columns if c in NORMALIZED_COLUMNS]
    out = out[recognized].copy()

    for col in NORMALIZED_COLUMNS:
        if col not in out.columns:
            out[col] = np.nan

    # Monthly workflow: month is chosen in the Streamlit sidebar. Legacy files
    # with billMonth are still accepted, but the sidebar month is used when it is
    # not present.
    if out["billMonth"].isna().all():
        if default_month is None:
            raise ValueError("Select the Gas bill month in the sidebar before uploading the gas file.")
        out["billMonth"] = pd.Timestamp(default_month)

    out["billMonth"] = pd.to_datetime(out["billMonth"], errors="coerce").dt.to_period("M").dt.to_timestamp()
    if out["billMonth"].isna().any():
        bad = int(out["billMonth"].isna().sum())
        raise ValueError(f"{bad} gas row(s) have an invalid bill month.")

    out["utility_account"] = out["utility_account"].map(_clean_string).fillna("")
    out = out[out["utility_account"].ne("")].copy()

    out["therms"] = pd.to_numeric(
        out["therms"].astype(str).str.replace(",", "", regex=False).str.replace("$", "", regex=False),
        errors="coerce",
    )
    out["cost"] = pd.to_numeric(
        out["cost"].astype(str).str.replace(",", "", regex=False).str.replace("$", "", regex=False),
        errors="coerce",
    )

    invalid = out[out["therms"].isna() | out["cost"].isna()]
    if not invalid.empty:
        accounts = ", ".join(invalid["utility_account"].astype(str).head(10).tolist())
        raise ValueError(
            f"{len(invalid)} gas row(s) have a non-numeric Cost or Therms value. "
            f"Check Utility Account(s): {accounts}."
        )

    # Reject duplicate accounts in one monthly file. Each provider account
    # should appear once; this prevents an accidental double import.
    duplicated = out[out.duplicated(subset=["utility_account", "billMonth"], keep=False)]
    if not duplicated.empty:
        accounts = ", ".join(sorted(duplicated["utility_account"].astype(str).unique().tolist())[:10])
        raise ValueError(
            "The gas upload contains duplicate Utility Account rows for the same month: "
            f"{accounts}. Combine each account to one row before uploading."
        )

    out = _apply_reference_data(out, gas_accounts)

    out["provider"] = out["provider"].map(_clean_string).fillna("").str.upper()
    out["erau_account"] = out["erau_account"].map(_clean_string)
    out["account_name"] = out["account_name"].map(_clean_string)
    out["building_id"] = out["building_id"].map(normalize_building_id)
    out["gas_meter_id"] = out["gas_meter_id"].map(_clean_string)
    out["rate_schedule"] = out["rate_schedule"].map(_clean_string)
    out["usage_group"] = out["usage_group"].map(_clean_string)
    out["cost_component"] = out["cost_component"].map(_clean_string)
    out["end_use"] = out["end_use"].map(_clean_string)
    out["notes"] = out["notes"].map(_clean_string)

    out["lineCostPerTherm"] = out["cost"] / out["therms"].where(out["therms"] > 0)
    return out[NORMALIZED_COLUMNS + ["lineCostPerTherm"]].reset_index(drop=True)


def load_gas_file(
    source: bytes | BinaryIO,
    source_name: str = "gas_upload.xlsx",
    default_month=None,
    gas_accounts: pd.DataFrame | None = None,
) -> GasLoadResult:
    table = _read_table(source, source_name)
    return GasLoadResult(
        rows=normalize_gas_upload(table, default_month=default_month, gas_accounts=gas_accounts),
        source_name=source_name,
    )


def gas_template_excel() -> bytes:
    """Return the minimal monthly gas upload template."""
    output = BytesIO()
    template = pd.DataFrame(columns=["Utility Account", "Cost", "Therms"])
    instructions = pd.DataFrame(
        {
            "Item": [
                "Utility Account",
                "Cost",
                "Therms",
                "Gas Bill Month",
                "Account Mapping",
            ],
            "Guidance": [
                "Required. Enter the TECO, Gas South, or other utility account number exactly as registered in the dashboard.",
                "Required. Total invoice cost in dollars for that utility account for the selected month.",
                "Required. Therms shown on the invoice. Enter 0 when there is no usage but a fixed charge remains.",
                "Do not add this as a spreadsheet column. Select the month in the Streamlit sidebar before uploading.",
                "Provider, building, meter, rate, end use, and TECO/Gas South pairing are looked up automatically from Reference Data > Gas Accounts.",
            ],
        }
    )
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        template.to_excel(writer, sheet_name="Gas History", index=False)
        instructions.to_excel(writer, sheet_name="Instructions", index=False)

        # Keep the template easy to use without adding visual complexity.
        ws = writer.book["Gas History"]
        ws.freeze_panes = "A2"
        ws.column_dimensions["A"].width = 24
        ws.column_dimensions["B"].width = 16
        ws.column_dimensions["C"].width = 16
        wi = writer.book["Instructions"]
        wi.column_dimensions["A"].width = 22
        wi.column_dimensions["B"].width = 105

    return output.getvalue()
