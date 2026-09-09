from __future__ import annotations

from pathlib import Path
import re
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "data" / "electric_accounts_seed.csv"

def normalize_account_number(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    if re.search(r"[eE][+-]?\d+", text):
        try:
            return str(int(float(text))).zfill(10)
        except Exception:
            pass
    digits = re.sub(r"\D", "", text)
    return digits.zfill(10) if digits else ""

def load_electric_accounts(path: str | Path | None = None) -> pd.DataFrame:
    source = Path(path) if path else DEFAULT_PATH
    df = pd.read_csv(source, dtype={"account_number": "string", "building_id": "string"})
    df["account_number"] = df["account_number"].map(normalize_account_number)
    df["building_id"] = df["building_id"].astype(str).str.strip()
    df["allocation_pct"] = pd.to_numeric(df.get("allocation_pct", 1.0), errors="coerce").fillna(1.0)
    df["gsf"] = pd.to_numeric(df.get("gsf"), errors="coerce")
    if "active" in df.columns:
        df = df[df["active"].astype(str).str.lower().isin({"true","1","yes","y"})].copy()
    return df
