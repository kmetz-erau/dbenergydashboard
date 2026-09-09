from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DEFAULT_DB = f"sqlite:///{(DATA_DIR / 'dashboard.db').as_posix()}"

metadata = MetaData()

buildings_table = Table(
    "buildings",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("building_id", String(100), nullable=False, unique=True),
    Column("property_id", String(100), nullable=True),
    Column("campus", String(200), nullable=False, default="DB"),
    Column("facility_name", String(300), nullable=True),
    Column("use_type", String(300), nullable=True),
    Column("area_sqft", Float, nullable=True),
    Column("eui_category", String(100), nullable=False, default="Normal Building"),
    Column("active", Boolean, nullable=False, default=True),
    Column("notes", String(1000), nullable=True),
)

meters_table = Table(
    "meters",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("campus", String(50), nullable=False, default="DB"),
    Column("meter_id", String(100), nullable=False),
    Column("building_id", String(100), nullable=False),
    Column("utility", String(50), nullable=False, default="Electricity"),
    Column("rate_schedule", String(200), nullable=True),
    Column("allocation_pct", Float, nullable=True),
    Column("status", String(100), nullable=False, default="Current"),
    Column("active", Boolean, nullable=False, default=True),
    Column("notes", String(1000), nullable=True),
)


electric_history_table = Table(
    "electric_history",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("bill_key", String(300), nullable=False, unique=True),
    Column("account_number", String(100), nullable=False),
    Column("account_name", String(300), nullable=True),
    Column("service_address", String(500), nullable=True),
    Column("meter_number", String(100), nullable=True),
    Column("full_meter_id", String(100), nullable=True),
    Column("date_billed", DateTime, nullable=False),
    Column("period_start", DateTime, nullable=True),
    Column("period_end", DateTime, nullable=True),
    Column("days_used", Float, nullable=True),
    Column("kwh", Float, nullable=True),
    Column("new_charges", Float, nullable=True),
    Column("total_bill_amount", Float, nullable=True),
    Column("source_file", String(500), nullable=True),
)


demand_history_table = Table(
    "demand_history",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("demand_key", String(300), nullable=False, unique=True),
    Column("account_number", String(100), nullable=False),
    Column("account_name", String(300), nullable=True),
    Column("service_address", String(500), nullable=True),
    Column("meter_id", String(100), nullable=False),
    Column("date_billed", DateTime, nullable=False),
    Column("rate_schedule_bill", String(100), nullable=True),
    Column("bill_rate", String(2000), nullable=True),
    Column("generic_demand_kw", Float, nullable=True),
    Column("on_peak_demand_kw", Float, nullable=True),
    Column("maximum_demand_kw", Float, nullable=True),
    Column("billed_peak_demand_kw", Float, nullable=True),
    Column("demand_charge_per_kw", Float, nullable=True),
    Column("maximum_demand_charge_per_kw", Float, nullable=True),
    Column("demand_charge_amount", Float, nullable=True),
    Column("demand_charge_is_estimated", Boolean, nullable=False, default=False),
    Column("kwh", Float, nullable=True),
    Column("total_bill_amount", Float, nullable=True),
    Column("source_sheet", String(100), nullable=True),
    Column("source_file", String(500), nullable=True),
)

gas_accounts_table = Table(
    "gas_accounts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(100), nullable=False),
    Column("utility_account", String(100), nullable=False),
    Column("erau_account", String(100), nullable=True),
    Column("building_id", String(100), nullable=False),
    Column("account_name", String(300), nullable=True),
    Column("gas_meter_id", String(100), nullable=True),
    Column("rate_schedule", String(200), nullable=True),
    Column("usage_group", String(150), nullable=True),
    Column("cost_component", String(100), nullable=True),
    Column("end_use", String(100), nullable=False, default="Building"),
    Column("usage_authoritative", Boolean, nullable=False, default=True),
    Column("include_in_site_eui", Boolean, nullable=False, default=True),
    Column("active", Boolean, nullable=False, default=True),
    Column("notes", String(1000), nullable=True),
    UniqueConstraint("provider", "utility_account", name="uq_gas_provider_account"),
)


gas_history_table = Table(
    "gas_history",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("gas_key", String(350), nullable=False, unique=True),
    Column("provider", String(100), nullable=False),
    Column("utility_account", String(100), nullable=False),
    Column("erau_account", String(100), nullable=True),
    Column("account_name", String(300), nullable=True),
    Column("building_id_upload", String(100), nullable=True),
    Column("bill_month", DateTime, nullable=False),
    Column("therms", Float, nullable=True),
    Column("cost", Float, nullable=True),
    Column("gas_meter_id", String(100), nullable=True),
    Column("rate_schedule", String(200), nullable=True),
    Column("usage_group", String(150), nullable=True),
    Column("cost_component", String(100), nullable=True),
    Column("end_use", String(100), nullable=True),
    Column("notes", String(1000), nullable=True),
    Column("source_file", String(500), nullable=True),
)



def get_engine(database_url: str | None = None) -> Engine:
    url = database_url or os.getenv("DATABASE_URL") or DEFAULT_DB
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)


def init_database(engine: Engine, seed: bool = True) -> None:
    """Create any missing schema objects without deleting existing data.

    This intentionally runs safely against an existing database. SQLAlchemy's
    ``create_all`` only creates missing tables, which makes it suitable for the
    additive schema changes used by this app (for example, demand_history).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    metadata.create_all(engine, checkfirst=True)
    if seed:
        _seed_if_empty(engine)


def ensure_demand_history_table(engine: Engine) -> None:
    """Ensure the demand-history table exists before any demand operation.

    Streamlit can keep a cached Engine alive across a Git hot-reload. If a new
    table is added in a deployment, the cached Engine may survive while the
    one-time initialization function does not run again. Creating this table
    with ``checkfirst=True`` is idempotent and protects upgrades from older DBs.
    """
    demand_history_table.create(engine, checkfirst=True)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _datetime_or_none(value: Any):
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.to_pydatetime()


def _none_if_nan(value: Any) -> Any:
    return None if pd.isna(value) else value


def _seed_if_empty(engine: Engine) -> None:
    with engine.begin() as conn:
        building_count = conn.execute(select(buildings_table.c.id).limit(1)).first()
        meter_count = conn.execute(select(meters_table.c.id).limit(1)).first()
        gas_account_count = conn.execute(select(gas_accounts_table.c.id).limit(1)).first()

        if building_count is None:
            df = pd.read_csv(DATA_DIR / "buildings_seed.csv", dtype={"property_id": "string"})
            rows = []
            for row in df.to_dict("records"):
                rows.append({
                    "building_id": str(row["building_id"]).strip(),
                    "property_id": _none_if_nan(row.get("property_id")),
                    "campus": str(row.get("campus") or "DB").strip(),
                    "facility_name": _none_if_nan(row.get("facility_name")),
                    "use_type": _none_if_nan(row.get("use_type")),
                    "area_sqft": _none_if_nan(row.get("area_sqft")),
                    "eui_category": str(row.get("eui_category") or "Normal Building").strip(),
                    "active": _coerce_bool(row.get("active", True)),
                    "notes": _none_if_nan(row.get("notes")),
                })
            if rows:
                conn.execute(insert(buildings_table), rows)

        if meter_count is None:
            df = pd.read_csv(DATA_DIR / "meters_seed.csv")
            rows = []
            for row in df.to_dict("records"):
                rows.append({
                    "campus": str(row.get("campus") or "DB").strip(),
                    "meter_id": str(row["meter_id"]).strip().upper(),
                    "building_id": str(row["building_id"]).strip(),
                    "utility": str(row.get("utility") or "Electricity").strip(),
                    "rate_schedule": _none_if_nan(row.get("rate_schedule")),
                    "allocation_pct": _none_if_nan(row.get("allocation_pct")),
                    "status": str(row.get("status") or "Current").strip(),
                    "active": _coerce_bool(row.get("active", True)),
                    "notes": _none_if_nan(row.get("notes")),
                })
            if rows:
                conn.execute(insert(meters_table), rows)

        # Gas reference seeds are additive. Insert only missing provider/account
        # pairs on every startup so historical account mappings added in later
        # releases become available without overwriting user-edited records.
        seed_path = DATA_DIR / "gas_accounts_seed.csv"
        if seed_path.exists():
            df = pd.read_csv(seed_path, dtype={"utility_account": "string", "erau_account": "string"})
            existing_gas = set(
                (str(r[0]).strip().upper(), str(r[1]).strip())
                for r in conn.execute(select(gas_accounts_table.c.provider, gas_accounts_table.c.utility_account)).all()
            )
            rows = []
            for row in df.to_dict("records"):
                provider = str(row.get("provider") or "").strip().upper()
                utility_account = str(row.get("utility_account") or "").strip()
                if not provider or not utility_account or (provider, utility_account) in existing_gas:
                    continue
                rows.append({
                    "provider": provider,
                    "utility_account": utility_account,
                    "erau_account": _none_if_nan(row.get("erau_account")),
                    "building_id": str(row.get("building_id") or "").strip(),
                    "account_name": _none_if_nan(row.get("account_name")),
                    "gas_meter_id": _none_if_nan(row.get("gas_meter_id")),
                    "rate_schedule": _none_if_nan(row.get("rate_schedule")),
                    "usage_group": _none_if_nan(row.get("usage_group")),
                    "cost_component": _none_if_nan(row.get("cost_component")),
                    "end_use": str(row.get("end_use") or "Building").strip(),
                    "usage_authoritative": _coerce_bool(row.get("usage_authoritative", True)),
                    "include_in_site_eui": _coerce_bool(row.get("include_in_site_eui", True)),
                    "active": _coerce_bool(row.get("active", True)),
                    "notes": _none_if_nan(row.get("notes")),
                })
                existing_gas.add((provider, utility_account))
            if rows:
                conn.execute(insert(gas_accounts_table), rows)


def load_buildings(engine: Engine, include_inactive: bool = True) -> pd.DataFrame:
    stmt = select(buildings_table)
    if not include_inactive:
        stmt = stmt.where(buildings_table.c.active.is_(True))
    with engine.connect() as conn:
        rows = conn.execute(stmt.order_by(buildings_table.c.building_id)).mappings().all()
    return pd.DataFrame(rows)


def load_meters(engine: Engine, include_inactive: bool = True) -> pd.DataFrame:
    stmt = select(meters_table)
    if not include_inactive:
        stmt = stmt.where(meters_table.c.active.is_(True))
    with engine.connect() as conn:
        rows = conn.execute(stmt.order_by(meters_table.c.meter_id, meters_table.c.building_id)).mappings().all()
    return pd.DataFrame(rows)


def add_building(engine: Engine, values: dict[str, Any]) -> int:
    payload = dict(values)
    payload["building_id"] = str(payload["building_id"]).strip()
    payload["active"] = bool(payload.get("active", True))
    with engine.begin() as conn:
        result = conn.execute(insert(buildings_table).values(**payload))
        return int(result.inserted_primary_key[0])


def update_building(engine: Engine, row_id: int, values: dict[str, Any]) -> None:
    payload = dict(values)
    payload.pop("id", None)
    if "building_id" in payload:
        payload["building_id"] = str(payload["building_id"]).strip()
    with engine.begin() as conn:
        conn.execute(update(buildings_table).where(buildings_table.c.id == row_id).values(**payload))


def add_meter(engine: Engine, values: dict[str, Any]) -> int:
    payload = dict(values)
    payload["meter_id"] = str(payload["meter_id"]).strip().upper()
    payload["building_id"] = str(payload["building_id"]).strip()
    payload["active"] = bool(payload.get("active", True))
    with engine.begin() as conn:
        result = conn.execute(insert(meters_table).values(**payload))
        return int(result.inserted_primary_key[0])


def update_meter(engine: Engine, row_id: int, values: dict[str, Any]) -> None:
    payload = dict(values)
    payload.pop("id", None)
    if "meter_id" in payload:
        payload["meter_id"] = str(payload["meter_id"]).strip().upper()
    with engine.begin() as conn:
        conn.execute(update(meters_table).where(meters_table.c.id == row_id).values(**payload))


def delete_meter(engine: Engine, row_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(delete(meters_table).where(meters_table.c.id == row_id))


ELECTRIC_APP_COLUMNS = [
    "accountNumber", "accountName", "serviceAddress", "meterNumber", "fullMeterID",
    "dateBilled", "periodStart", "periodEnd", "daysUsed", "kWh", "newCharges",
    "totalBillAmount", "sourceFile",
]


def ensure_electric_history_table(engine: Engine) -> None:
    electric_history_table.create(engine, checkfirst=True)


def upsert_electric_history(engine: Engine, bills: pd.DataFrame, source_file: str | None = None) -> dict[str, int]:
    """Persist normalized FPL bills using account + meter + bill date as the durable key."""
    ensure_electric_history_table(engine)
    if bills is None or bills.empty:
        return {"inserted": 0, "updated": 0}
    inserted_count = 0
    updated_count = 0
    with engine.begin() as conn:
        for row in bills.to_dict("records"):
            account = str(row.get("accountNumber") or "").strip()
            meter_id = str(row.get("fullMeterID") or "").strip().upper()
            date_billed = pd.to_datetime(row.get("dateBilled"), errors="coerce")
            if not account or pd.isna(date_billed):
                continue
            bill_key = f"{account}|{date_billed:%Y-%m-%d}"
            payload = {
                "bill_key": bill_key,
                "account_number": account,
                "account_name": _none_if_nan(row.get("accountName")),
                "service_address": _none_if_nan(row.get("serviceAddress")),
                "meter_number": _none_if_nan(row.get("meterNumber")),
                "full_meter_id": meter_id,
                "date_billed": date_billed.to_pydatetime(),
                "period_start": _datetime_or_none(row.get("periodStart")),
                "period_end": _datetime_or_none(row.get("periodEnd")),
                "days_used": _none_if_nan(row.get("daysUsed")),
                "kwh": _none_if_nan(row.get("kWh")),
                "new_charges": _none_if_nan(row.get("newCharges")),
                "total_bill_amount": _none_if_nan(row.get("totalBillAmount")),
                "source_file": source_file,
            }
            existing = conn.execute(select(electric_history_table.c.id).where(electric_history_table.c.bill_key == bill_key)).first()
            if existing is None:
                conn.execute(insert(electric_history_table).values(**payload))
                inserted_count += 1
            else:
                conn.execute(update(electric_history_table).where(electric_history_table.c.bill_key == bill_key).values(**payload))
                updated_count += 1
    return {"inserted": inserted_count, "updated": updated_count}


def load_electric_history(engine: Engine) -> pd.DataFrame:
    ensure_electric_history_table(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            select(electric_history_table).order_by(electric_history_table.c.date_billed, electric_history_table.c.account_number)
        ).mappings().all()
    if not rows:
        return pd.DataFrame(columns=ELECTRIC_APP_COLUMNS)
    df = pd.DataFrame(rows).rename(columns={
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
    })
    for col in ["dateBilled", "periodStart", "periodEnd"]:
        df[col] = pd.to_datetime(df[col], errors="coerce")
    return df[[c for c in ELECTRIC_APP_COLUMNS if c in df.columns]]


DEMAND_APP_COLUMNS = [
    "accountNumber", "accountName", "serviceAddress", "dateBilled", "billMonth",
    "fullMeterID", "rateScheduleBill", "billRate", "genericDemandKW",
    "onPeakDemandKW", "maximumDemandKW", "billedPeakDemandKW",
    "demandChargePerKW", "maximumDemandChargePerKW", "demandChargeAmount",
    "demandChargeIsEstimated", "kWh", "totalBillAmount", "sourceSheet", "sourceFile",
]


def upsert_demand_history(engine: Engine, demand: pd.DataFrame, source_file: str | None = None) -> dict[str, int]:
    """Persist normalized demand rows so month-over-month demand history grows with uploads."""
    ensure_demand_history_table(engine)
    if demand is None or demand.empty:
        return {"inserted": 0, "updated": 0}

    inserted = 0
    updated_count = 0
    with engine.begin() as conn:
        for row in demand.to_dict("records"):
            meter_id = str(row.get("fullMeterID") or "").strip().upper()
            account = str(row.get("accountNumber") or "").strip()
            date_billed = pd.to_datetime(row.get("dateBilled"), errors="coerce")
            if not meter_id or not account or pd.isna(date_billed):
                continue
            date_value = date_billed.to_pydatetime()
            demand_key = f"{account}|{meter_id}|{date_billed:%Y-%m-%d}"
            payload = {
                "demand_key": demand_key,
                "account_number": account,
                "account_name": _none_if_nan(row.get("accountName")),
                "service_address": _none_if_nan(row.get("serviceAddress")),
                "meter_id": meter_id,
                "date_billed": date_value,
                "rate_schedule_bill": _none_if_nan(row.get("rateScheduleBill")),
                "bill_rate": _none_if_nan(row.get("billRate")),
                "generic_demand_kw": _none_if_nan(row.get("genericDemandKW")),
                "on_peak_demand_kw": _none_if_nan(row.get("onPeakDemandKW")),
                "maximum_demand_kw": _none_if_nan(row.get("maximumDemandKW")),
                "billed_peak_demand_kw": _none_if_nan(row.get("billedPeakDemandKW")),
                "demand_charge_per_kw": _none_if_nan(row.get("demandChargePerKW")),
                "maximum_demand_charge_per_kw": _none_if_nan(row.get("maximumDemandChargePerKW")),
                "demand_charge_amount": _none_if_nan(row.get("demandChargeAmount")),
                "demand_charge_is_estimated": bool(row.get("demandChargeIsEstimated", False)),
                "kwh": _none_if_nan(row.get("kWh")),
                "total_bill_amount": _none_if_nan(row.get("totalBillAmount")),
                "source_sheet": _none_if_nan(row.get("sourceSheet")),
                "source_file": source_file,
            }
            existing = conn.execute(
                select(demand_history_table.c.id).where(demand_history_table.c.demand_key == demand_key)
            ).first()
            if existing is None:
                conn.execute(insert(demand_history_table).values(**payload))
                inserted += 1
            else:
                conn.execute(
                    update(demand_history_table)
                    .where(demand_history_table.c.demand_key == demand_key)
                    .values(**payload)
                )
                updated_count += 1
    return {"inserted": inserted, "updated": updated_count}


def load_demand_history(engine: Engine) -> pd.DataFrame:
    ensure_demand_history_table(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            select(demand_history_table).order_by(demand_history_table.c.date_billed, demand_history_table.c.account_number)
        ).mappings().all()
    if not rows:
        return pd.DataFrame(columns=DEMAND_APP_COLUMNS)
    df = pd.DataFrame(rows).rename(columns={
        "account_number": "accountNumber",
        "account_name": "accountName",
        "service_address": "serviceAddress",
        "meter_id": "fullMeterID",
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
    })
    df["dateBilled"] = pd.to_datetime(df["dateBilled"], errors="coerce")
    df["billMonth"] = df["dateBilled"].dt.to_period("M").dt.to_timestamp()
    return df[[c for c in DEMAND_APP_COLUMNS if c in df.columns]]


def ensure_gas_tables(engine: Engine) -> None:
    """Idempotently create the additive gas schema for older deployments."""
    gas_accounts_table.create(engine, checkfirst=True)
    gas_history_table.create(engine, checkfirst=True)


def load_gas_accounts(engine: Engine, include_inactive: bool = True) -> pd.DataFrame:
    ensure_gas_tables(engine)
    stmt = select(gas_accounts_table)
    if not include_inactive:
        stmt = stmt.where(gas_accounts_table.c.active.is_(True))
    with engine.connect() as conn:
        rows = conn.execute(stmt.order_by(gas_accounts_table.c.building_id, gas_accounts_table.c.provider)).mappings().all()
    return pd.DataFrame(rows)


def add_gas_account(engine: Engine, values: dict[str, Any]) -> int:
    ensure_gas_tables(engine)
    payload = dict(values)
    payload["provider"] = str(payload["provider"]).strip().upper()
    payload["utility_account"] = str(payload["utility_account"]).strip()
    payload["building_id"] = str(payload["building_id"]).strip()
    payload["active"] = bool(payload.get("active", True))
    payload["usage_authoritative"] = bool(payload.get("usage_authoritative", True))
    payload["include_in_site_eui"] = bool(payload.get("include_in_site_eui", True))
    with engine.begin() as conn:
        result = conn.execute(insert(gas_accounts_table).values(**payload))
        return int(result.inserted_primary_key[0])


def update_gas_account(engine: Engine, row_id: int, values: dict[str, Any]) -> None:
    ensure_gas_tables(engine)
    payload = dict(values)
    payload.pop("id", None)
    if "provider" in payload:
        payload["provider"] = str(payload["provider"]).strip().upper()
    if "utility_account" in payload:
        payload["utility_account"] = str(payload["utility_account"]).strip()
    with engine.begin() as conn:
        conn.execute(update(gas_accounts_table).where(gas_accounts_table.c.id == row_id).values(**payload))


def delete_gas_account(engine: Engine, row_id: int) -> None:
    ensure_gas_tables(engine)
    with engine.begin() as conn:
        conn.execute(delete(gas_accounts_table).where(gas_accounts_table.c.id == row_id))


GAS_HISTORY_APP_COLUMNS = [
    "provider", "utility_account", "erau_account", "account_name", "building_id",
    "billMonth", "therms", "cost", "gas_meter_id", "rate_schedule", "usage_group",
    "cost_component", "end_use", "notes", "sourceFile",
]

def upsert_gas_history(engine: Engine, gas_rows: pd.DataFrame, source_file: str | None = None) -> dict[str, int]:
    ensure_gas_tables(engine)
    if gas_rows is None or gas_rows.empty:
        return {"inserted": 0, "updated": 0}
    inserted = 0
    updated_count = 0
    with engine.begin() as conn:
        for row in gas_rows.to_dict("records"):
            provider = str(row.get("provider") or "").strip().upper()
            account = str(row.get("utility_account") or "").strip()
            month = pd.to_datetime(row.get("billMonth"), errors="coerce")
            if not provider or not account or pd.isna(month):
                continue
            month = month.to_period("M").to_timestamp()
            gas_key = f"{provider}|{account}|{month:%Y-%m}"
            payload = {
                "gas_key": gas_key,
                "provider": provider,
                "utility_account": account,
                "erau_account": _none_if_nan(row.get("erau_account")),
                "account_name": _none_if_nan(row.get("account_name")),
                "building_id_upload": _none_if_nan(row.get("building_id")),
                "bill_month": month.to_pydatetime(),
                "therms": _none_if_nan(row.get("therms")),
                "cost": _none_if_nan(row.get("cost")),
                "gas_meter_id": _none_if_nan(row.get("gas_meter_id")),
                "rate_schedule": _none_if_nan(row.get("rate_schedule")),
                "usage_group": _none_if_nan(row.get("usage_group")),
                "cost_component": _none_if_nan(row.get("cost_component")),
                "end_use": _none_if_nan(row.get("end_use")),
                "notes": _none_if_nan(row.get("notes")),
                "source_file": source_file,
            }
            existing = conn.execute(select(gas_history_table.c.id).where(gas_history_table.c.gas_key == gas_key)).first()
            if existing is None:
                conn.execute(insert(gas_history_table).values(**payload))
                inserted += 1
            else:
                conn.execute(update(gas_history_table).where(gas_history_table.c.gas_key == gas_key).values(**payload))
                updated_count += 1
    return {"inserted": inserted, "updated": updated_count}


def load_gas_history(engine: Engine) -> pd.DataFrame:
    ensure_gas_tables(engine)
    with engine.connect() as conn:
        rows = conn.execute(select(gas_history_table).order_by(gas_history_table.c.bill_month, gas_history_table.c.provider)).mappings().all()
    if not rows:
        return pd.DataFrame(columns=GAS_HISTORY_APP_COLUMNS)
    df = pd.DataFrame(rows).rename(columns={
        "building_id_upload": "building_id",
        "bill_month": "billMonth",
        "source_file": "sourceFile",
    })
    df["billMonth"] = pd.to_datetime(df["billMonth"], errors="coerce").dt.to_period("M").dt.to_timestamp()
    return df[[c for c in GAS_HISTORY_APP_COLUMNS if c in df.columns]]
