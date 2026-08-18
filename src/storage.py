from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
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


def get_engine(database_url: str | None = None) -> Engine:
    url = database_url or os.getenv("DATABASE_URL") or DEFAULT_DB
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)


def init_database(engine: Engine, seed: bool = True) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    metadata.create_all(engine)
    if seed:
        _seed_if_empty(engine)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _none_if_nan(value: Any) -> Any:
    return None if pd.isna(value) else value


def _seed_if_empty(engine: Engine) -> None:
    with engine.begin() as conn:
        building_count = conn.execute(select(buildings_table.c.id).limit(1)).first()
        meter_count = conn.execute(select(meters_table.c.id).limit(1)).first()

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
