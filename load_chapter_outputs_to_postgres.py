#!/usr/bin/env python3
"""
Load Chapter 3 and Chapter 4 pipeline CSV outputs into PostgreSQL.

This is intentionally separate from the chapter pipelines. The chapter scripts can
keep generating CSV backups, and this loader sends those generated tables to the
PostgreSQL database used by Grafana.

Default container paths:
    Chapter 3 CSV outputs: /outputs/chapter3
    Chapter 4 CSV outputs: /outputs/chapter4

Required PostgreSQL environment variables, unless POSTGRES_URL is provided:
    POSTGRES_HOST
    POSTGRES_PORT
    POSTGRES_DB
    POSTGRES_USER
    POSTGRES_PASSWORD
    POSTGRES_SCHEMA    optional; defaults to public

Alternative:
    POSTGRES_URL=postgresql+psycopg2://user:password@host:5432/dbname
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote_plus

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

LOGGER = logging.getLogger(__name__)

DEFAULT_CHAPTER3_DIR = Path("/outputs/chapter3")
DEFAULT_CHAPTER4_DIR = Path("/outputs/chapter4")
DEFAULT_SCHEMA = "public"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def postgres_url_from_env() -> str:
    """Build a SQLAlchemy PostgreSQL URL from environment variables."""
    direct_url = os.getenv("POSTGRES_URL")
    if direct_url:
        return direct_url

    host = require_env("POSTGRES_HOST")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = require_env("POSTGRES_DB")
    user = require_env("POSTGRES_USER")
    password = require_env("POSTGRES_PASSWORD")

    return (
        "postgresql+psycopg2://"
        f"{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{quote_plus(db)}"
    )


def get_schema(cli_schema: Optional[str] = None) -> str:
    schema = cli_schema or os.getenv("POSTGRES_SCHEMA") or DEFAULT_SCHEMA
    if not _IDENTIFIER_RE.match(schema):
        raise ValueError(
            f"Unsafe PostgreSQL schema name {schema!r}. Use letters, numbers, and underscores only."
        )
    return schema


def make_engine() -> Engine:
    return create_engine(postgres_url_from_env(), pool_pre_ping=True)


def create_schema_if_needed(engine: Engine, schema: str) -> None:
    if not _IDENTIFIER_RE.match(schema):
        raise ValueError(f"Unsafe PostgreSQL schema name: {schema!r}")
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))


def clean_table_name(name: str, prefix: str = "") -> str:
    """Convert a file stem to a safe PostgreSQL table name."""
    base = name.strip().lower()
    base = re.sub(r"[^a-z0-9]+", "_", base)
    base = re.sub(r"_+", "_", base).strip("_")

    prefix = prefix.strip().lower().strip("_")
    if prefix and not base.startswith(prefix + "_"):
        base = f"{prefix}_{base}"

    if not base:
        raise ValueError(f"Could not create a table name from {name!r}")
    if base[0].isdigit():
        base = f"t_{base}"

    # PostgreSQL identifiers are limited to 63 bytes. Keep names simple.
    return base[:63]


def coerce_obvious_dates(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert obvious date/time columns to pandas datetime so PostgreSQL receives
    timestamp/date-like columns instead of plain text where possible.
    """
    out = df.copy()
    for col in out.columns:
        key = str(col).strip().lower()
        likely_date = (
            key in {"date", "day", "feeding_date", "feeding_date_std", "file_date"}
            or key.endswith("_date")
            or key.endswith(" date")
            or "run_date" in key
            or "run date" in key
            or "datetime" in key
            or "date_time" in key
            or "date time" in key
        )
        if likely_date:
            converted = pd.to_datetime(out[col], errors="coerce")
            # Only keep conversion if it worked for at least one non-null value.
            if converted.notna().sum() > 0:
                out[col] = converted
    return out


def read_csv_for_postgres(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, low_memory=False)
    df = coerce_obvious_dates(df)
    return df


def write_dataframe_to_postgres(
    df: pd.DataFrame,
    table_name: str,
    engine: Engine,
    schema: str,
    if_exists: str = "replace",
    chunksize: int = 10000,
) -> None:
    if df.empty:
        LOGGER.warning("Table %s is empty; writing empty table structure.", table_name)

    df.to_sql(
        name=table_name,
        con=engine,
        schema=schema,
        if_exists=if_exists,
        index=False,
        method="multi",
        chunksize=chunksize,
    )


def iter_csv_files(path: Path) -> Iterable[Path]:
    if not path.exists():
        LOGGER.warning("Output directory does not exist, skipping: %s", path)
        return []
    return sorted(p for p in path.glob("*.csv") if p.is_file())


def load_csv_directory(
    directory: Path,
    table_prefix: str,
    engine: Engine,
    schema: str,
    if_exists: str = "replace",
    chunksize: int = 10000,
) -> list[str]:
    written: list[str] = []
    for csv_path in iter_csv_files(directory):
        table_name = clean_table_name(csv_path.stem, prefix=table_prefix)
        LOGGER.info("Loading %s -> %s.%s", csv_path, schema, table_name)
        df = read_csv_for_postgres(csv_path)
        write_dataframe_to_postgres(
            df=df,
            table_name=table_name,
            engine=engine,
            schema=schema,
            if_exists=if_exists,
            chunksize=chunksize,
        )
        LOGGER.info("Loaded %s rows into %s.%s", len(df), schema, table_name)
        written.append(table_name)
    return written


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Load Chapter 3 and Chapter 4 generated CSV outputs into PostgreSQL."
    )
    parser.add_argument(
        "--chapter3-dir",
        type=Path,
        default=DEFAULT_CHAPTER3_DIR,
        help="Directory containing Chapter 3 CSV outputs. Default: /outputs/chapter3",
    )
    parser.add_argument(
        "--chapter4-dir",
        type=Path,
        default=DEFAULT_CHAPTER4_DIR,
        help="Directory containing Chapter 4 CSV outputs. Default: /outputs/chapter4",
    )
    parser.add_argument(
        "--schema",
        default=None,
        help="PostgreSQL schema. Default: POSTGRES_SCHEMA environment variable, then public.",
    )
    parser.add_argument(
        "--if-exists",
        choices=["replace", "append", "fail"],
        default="replace",
        help="Behavior when a table already exists. Default: replace.",
    )
    parser.add_argument(
        "--chunksize",
        type=int,
        default=10000,
        help="Rows per insert batch. Default: 10000.",
    )
    parser.add_argument(
        "--skip-chapter3",
        action="store_true",
        help="Do not load Chapter 3 outputs.",
    )
    parser.add_argument(
        "--skip-chapter4",
        action="store_true",
        help="Do not load Chapter 4 outputs.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level. Default: INFO.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    schema = get_schema(args.schema)
    engine = make_engine()
    create_schema_if_needed(engine, schema)

    all_written: list[str] = []
    if not args.skip_chapter3:
        all_written.extend(
            load_csv_directory(
                directory=args.chapter3_dir,
                table_prefix="chapter3",
                engine=engine,
                schema=schema,
                if_exists=args.if_exists,
                chunksize=args.chunksize,
            )
        )

    if not args.skip_chapter4:
        all_written.extend(
            load_csv_directory(
                directory=args.chapter4_dir,
                table_prefix="chapter4",
                engine=engine,
                schema=schema,
                if_exists=args.if_exists,
                chunksize=args.chunksize,
            )
        )

    LOGGER.info("Done. Tables written: %s", ", ".join(all_written) or "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
