from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import BinaryIO, NamedTuple
import re

import numpy as np
import pandas as pd


# Internal normalized schema. The preferred upload is one cumulative Gas History
# workbook with Billing Month, Utility Account, Cost, and Therms. The remaining
# fields are resolved from Reference Data -> Gas Accounts.
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




_MONTHS = {
    "JAN": 1, "JANUARY": 1,
    "FEB": 2, "FEBRUARY": 2,
    "MAR": 3, "MARCH": 3,
    "APR": 4, "APRIL": 4,
    "MAY": 5,
    "JUN": 6, "JUNE": 6,
    "JUL": 7, "JULY": 7,
    "AUG": 8, "AUGUST": 8,
    "SEP": 9, "SEPT": 9, "SEPTEMBER": 9,
    "OCT": 10, "OCTOBER": 10,
    "NOV": 11, "NOVEMBER": 11,
    "DEC": 12, "DECEMBER": 12,
}


def _month_number(value: object) -> int | None:
    text = _clean_header(value).upper()
    if not text:
        return None
    for token, number in _MONTHS.items():
        if re.search(rf"\b{re.escape(token)}\b", text):
            return number
    return None


def _numeric_cell(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).replace("\xa0", " ").strip()
    if not text or text in {"-", "—", "–"}:
        return None
    text = text.replace("$", "").replace(",", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def _year_hint_from_row(row: pd.Series) -> int | None:
    text = " ".join(str(v) for v in row.tolist() if v is not None and not pd.isna(v))
    match = re.search(r"\b(?:19|20)(\d{2})\b", text)
    if match:
        return int(match.group(0))
    return None


def _fiscal_year_hint(raw: pd.DataFrame, row_idx: int) -> tuple[int, int] | None:
    for r in range(row_idx, max(-1, row_idx - 12), -1):
        text = " ".join(str(v) for v in raw.iloc[r].tolist() if v is not None and not pd.isna(v))
        m = re.search(r"FISCAL\s+YEAR\s*[’']?\s*(\d{2})\s*/\s*(\d{2})", text, re.I)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            return 2000 + a, 2000 + b
    return None


def _resolve_month_year(raw: pd.DataFrame, header_row: int, col_idx: int, section_year: int | None) -> pd.Timestamp | None:
    month = None
    # Search upward in the same column; this also lets the Gas South block reuse
    # JULY/AUG labels printed in the top TECO header of the user's workbook.
    for r in range(header_row - 1, -1, -1):
        month = _month_number(raw.iat[r, col_idx])
        if month:
            break
    if not month:
        return None

    if section_year:
        year = section_year
    else:
        fy = _fiscal_year_hint(raw, header_row)
        if fy:
            # Standard fiscal-year interpretation when there is no explicit
            # account-year hint: Jul-Dec = first year, Jan-Jun = second year.
            year = fy[0] if month >= 7 else fy[1]
        else:
            year = pd.Timestamp.today().year
    return pd.Timestamp(year=year, month=month, day=1)


def parse_wide_gas_workbook(raw: pd.DataFrame) -> pd.DataFrame:
    """Parse ERAU's wide TECO/GAS SOUTH workbook into normalized monthly rows.

    Expected shape is the user's existing worksheet style: section title,
    two-row headers, one account per row, and repeated MONTH THERMS / MONTH COST
    column pairs. TOTALS and fiscal-year separator rows are ignored.
    """
    if raw is None or raw.empty:
        return pd.DataFrame()

    rows: list[dict] = []
    nrows, ncols = raw.shape
    section_starts: list[tuple[int, str]] = []

    for r in range(nrows):
        text = " ".join(str(v) for v in raw.iloc[r].tolist() if v is not None and not pd.isna(v)).upper()
        if "ACCOUNT CODES - GAS" in text:
            provider = "GAS SOUTH" if "SOUTH" in text else "TECO"
            section_starts.append((r, provider))

    for start_idx, provider in section_starts:
        # Locate the account/header row directly after the section title.
        header_row = None
        for r in range(start_idx + 1, min(nrows, start_idx + 8)):
            vals = [_clean_header(v) for v in raw.iloc[r].tolist()]
            joined = " | ".join(vals)
            if ("name" in vals or "name" in joined) and ("acct" in joined or "account" in joined):
                header_row = r
                break
        if header_row is None:
            continue

        headers = [_clean_header(v) for v in raw.iloc[header_row].tolist()]
        bldg_col = next((i for i, h in enumerate(headers) if "bldg" in h or h == "building"), 0)
        name_col = next((i for i, h in enumerate(headers) if h == "name" or "name" in h), 1)
        account_col = next((i for i, h in enumerate(headers) if "acct" in h and "erau" not in h), 2)
        erau_col = next((i for i, h in enumerate(headers) if "erau" in h and "acct" in h), 3)

        # The account header itself usually contains an explicit year, e.g.
        # "Jan 2026 New TECO Acct#" or "June 2026 New Acct#". This is safer
        # for the user's GAS SOUTH block than relying solely on the fiscal label.
        section_year = _year_hint_from_row(raw.iloc[header_row])

        measure_cols: list[tuple[int, str, pd.Timestamp]] = []
        for c in range(ncols):
            h = headers[c] if c < len(headers) else ""
            if "therm" not in h and "cost" not in h:
                # GAS SOUTH in the user's sheet reuses the JULY/AUG columns but
                # does not repeat THERMS/COST on its local header. Reuse the
                # closest prior measure label in the same column.
                for prior in range(header_row - 1, -1, -1):
                    candidate = _clean_header(raw.iat[prior, c])
                    if "therm" in candidate or "cost" in candidate:
                        h = candidate
                        break
            if "therm" not in h and "cost" not in h:
                continue
            month = _resolve_month_year(raw, header_row, c, section_year)
            if month is None:
                continue
            measure = "therms" if "therm" in h else "cost"
            measure_cols.append((c, measure, month))

        if not measure_cols:
            continue

        month_map: dict[pd.Timestamp, dict[str, int]] = {}
        for c, measure, month in measure_cols:
            month_map.setdefault(month, {})[measure] = c

        for r in range(header_row + 1, nrows):
            text = " ".join(str(v) for v in raw.iloc[r].tolist() if v is not None and not pd.isna(v)).upper()
            if r > header_row + 1 and ("ACCOUNT CODES - GAS" in text or "FISCAL YEAR" in text):
                break
            if "TOTALS PER MONTH" in text:
                break

            account = _clean_string(raw.iat[r, account_col]) if account_col < ncols else None
            if not account:
                continue
            # Only account-like numeric identifiers are data rows.
            account_digits = re.sub(r"\D", "", account)
            if len(account_digits) < 6:
                continue

            building = _clean_string(raw.iat[r, bldg_col]) if bldg_col < ncols else None
            name = _clean_string(raw.iat[r, name_col]) if name_col < ncols else None
            erau = _clean_string(raw.iat[r, erau_col]) if erau_col < ncols else None

            for month, cols in month_map.items():
                therms = _numeric_cell(raw.iat[r, cols["therms"]]) if "therms" in cols else None
                cost = _numeric_cell(raw.iat[r, cols["cost"]]) if "cost" in cols else None
                # A blank pair means the provider did not bill that month; a
                # zero-therm row with a fixed cost must still be retained.
                if therms is None and cost is None:
                    continue
                rows.append({
                    "Billing Month": month,
                    "Provider": provider,
                    "Utility Account": account_digits,
                    "ERAU Account": erau,
                    "Name": name,
                    "Building": building,
                    "Therms": 0.0 if therms is None and cost is not None else therms,
                    "Cost": cost,
                })

    return pd.DataFrame(rows)


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
                "Gas account reference data is required for gas history uploads. Add the account under "
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
    """Normalize a cumulative gas-history upload.

    Preferred workbook columns:
      * Billing Month
      * Utility Account
      * Cost
      * Therms

    ``default_month`` is retained only for backwards compatibility with the older
    three-column monthly upload. Provider/building/meter metadata are looked up
    from the Gas Accounts reference table using Utility Account.
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
            + ". The history workbook requires Billing Month, Utility Account, Cost, and Therms."
        )

    # Keep only recognized columns so stray spreadsheet notes do not enter the
    # analytics pipeline.
    recognized = [c for c in out.columns if c in NORMALIZED_COLUMNS]
    out = out[recognized].copy()

    for col in NORMALIZED_COLUMNS:
        if col not in out.columns:
            out[col] = np.nan

    # Preferred workflow: every row carries its Billing Month so a single file
    # can hold all history. A supplied default_month keeps old three-column files
    # compatible, but the Streamlit UI no longer requires a separate month picker.
    if out["billMonth"].isna().all():
        if default_month is None:
            raise ValueError(
                "Gas history workbook is missing Billing Month. Required columns are "
                "Billing Month, Utility Account, Cost, and Therms."
            )
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
    raw = source if isinstance(source, bytes) else source.read()
    try:
        table = _read_table(raw, source_name)
        rows = normalize_gas_upload(table, default_month=default_month, gas_accounts=gas_accounts)
        return GasLoadResult(rows=rows, source_name=source_name)
    except ValueError as exc:
        # Fall back to the user's legacy wide workbook layout. CSV files do not
        # have the multi-row/section structure this parser expects.
        if Path(source_name).suffix.lower() not in {".xlsx", ".xls"}:
            raise
        engine = "xlrd" if Path(source_name).suffix.lower() == ".xls" else "openpyxl"
        excel = pd.ExcelFile(BytesIO(raw), engine=engine)
        wide = pd.read_excel(excel, sheet_name=excel.sheet_names[0], header=None, dtype=object)
        parsed = parse_wide_gas_workbook(wide)
        if parsed.empty:
            raise exc
        rows = normalize_gas_upload(parsed, default_month=default_month, gas_accounts=gas_accounts)
        return GasLoadResult(rows=rows, source_name=source_name)


def gas_template_excel() -> bytes:
    """Return the preferred cumulative gas-history upload template."""
    output = BytesIO()
    template = pd.DataFrame(columns=["Billing Month", "Utility Account", "Cost", "Therms"])
    instructions = pd.DataFrame(
        {
            "Item": [
                "Billing Month",
                "Utility Account",
                "Cost",
                "Therms",
                "Monthly Update",
                "Account Mapping",
            ],
            "Guidance": [
                "Required. Enter the billing month as a date, preferably the first day of the month (for example 8/1/2026).",
                "Required. Enter the TECO, Gas South, or other utility account number exactly as registered in the dashboard.",
                "Required. Total invoice cost in dollars for that utility account and billing month.",
                "Required. Therms shown on the invoice. Enter 0 when there is no usage but a fixed charge remains.",
                "Append each new month to the bottom of this same workbook. Keep all prior months and re-upload the full file.",
                "Provider, building, meter, rate, end use, and TECO/Gas South pairing are looked up automatically from Reference Data > Gas Accounts.",
            ],
        }
    )
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        template.to_excel(writer, sheet_name="Gas History", index=False)
        instructions.to_excel(writer, sheet_name="Instructions", index=False)
        ws = writer.book["Gas History"]
        ws.freeze_panes = "A2"
        ws.column_dimensions["A"].width = 16
        ws.column_dimensions["B"].width = 24
        ws.column_dimensions["C"].width = 16
        ws.column_dimensions["D"].width = 16
        wi = writer.book["Instructions"]
        wi.column_dimensions["A"].width = 22
        wi.column_dimensions["B"].width = 105
    return output.getvalue()


def gas_history_excel(history: pd.DataFrame | None) -> bytes:
    """Export stored gas history in the same four-column re-uploadable format."""
    output = BytesIO()
    if history is None or history.empty:
        export = pd.DataFrame(columns=["Billing Month", "Utility Account", "Cost", "Therms"])
    else:
        export = history.copy()
        export["Billing Month"] = pd.to_datetime(export.get("billMonth"), errors="coerce").dt.to_period("M").dt.to_timestamp()
        export["Utility Account"] = export.get("utility_account", pd.Series(index=export.index, dtype=object)).astype(str)
        export["Cost"] = pd.to_numeric(export.get("cost"), errors="coerce")
        export["Therms"] = pd.to_numeric(export.get("therms"), errors="coerce")
        export = export[["Billing Month", "Utility Account", "Cost", "Therms"]]
        export = export.sort_values(["Billing Month", "Utility Account"]).reset_index(drop=True)

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        export.to_excel(writer, sheet_name="Gas History", index=False)
        ws = writer.book["Gas History"]
        ws.freeze_panes = "A2"
        ws.column_dimensions["A"].width = 16
        ws.column_dimensions["B"].width = 24
        ws.column_dimensions["C"].width = 16
        ws.column_dimensions["D"].width = 16
        for row in range(2, ws.max_row + 1):
            ws.cell(row, 1).number_format = "mmm yyyy"
            ws.cell(row, 2).number_format = "@"
            ws.cell(row, 3).number_format = "$#,##0.00;[Red]($#,##0.00);-"
            ws.cell(row, 4).number_format = "#,##0.00;[Red](#,##0.00);-"
    return output.getvalue()
