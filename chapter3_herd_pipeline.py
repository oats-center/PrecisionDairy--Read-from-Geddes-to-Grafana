#!/usr/bin/env python3
"""
Chapter 3 Herd Characteristics Pipeline
=======================================

This single script consolidates the current Chapter 3 notebook logic into a reusable
pipeline. It reads Afimilk daily parquet files and an optional PCDart/body-weight CSV,
then creates the Chapter 3 output tables:

    table_3_1_yield_and_dim.csv
    table_3_2_dim_weights_merged.csv
    table_3_3_yield_group_day.csv
    table_3_4_yield_group_month.csv
    table_3_5_weight_group.csv
    per_cow_daily_yield_summary.csv

The statistical calculations follow Chapter 3:
- daily group summaries of milk yield and DIM;
- 80% t-confidence intervals around mean milk yield;
- monthly cow-level yield summarized to group-level monthly yield;
- monthly group-level body-weight summaries without confidence intervals;
- per-cow daily milk-yield confidence intervals.

Example
-------
python chapter3_herd_pipeline.py \
    --afi-dir /depot/agpasde/data/Reference/purdue/asrec/dairy/afimilk-daily \
    --weights-csv allWeights.csv \
    --output-dir chapter3_outputs \
    --start-date 2025-06-01 \
    --confidence 0.80

Notes
-----
- The script tries to recognize several common column names from the notebooks and
  exported files, including farm_animal_id/Cow ID, grp/Group, dim/DIM,
  daily_yield/Yield, and yield_s_1 + yield_s_2.
- If the Afimilk parquet files do not contain a date column, the date is extracted
  from the file name using YYYYMMDD or YYYY-MM-DD patterns.
- If the body-weight CSV has a Date column, year/month are taken from it. If it has
  only Month, use --weight-year, or the script will infer the year only when Afimilk
  data contain exactly one year.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd
from scipy import stats


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

DEFAULT_GROUP_MAPPING = {
    "pack": 15,
    "Pack": 15,
    "PACK": 15,
    "g1": 1,
    "G1": 1,
    "g2": 2,
    "G2": 2,
    "g3": 3,
    "G3": 3,
    "g4": 4,
    "G4": 4,
}

MONTH_NAME_TO_NUM = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


@dataclass(frozen=True)
class PipelineOutputs:
    output_dir: Path
    yield_and_dim: Path
    dim_weights_merged: Optional[Path]
    yield_group_day: Path
    yield_group_month: Path
    weight_group: Optional[Path]
    per_cow_daily_yield_summary: Path
    animal_month_yield: Path
    cow_month_weight: Optional[Path]
    run_summary: Path


# -----------------------------------------------------------------------------
# Utility functions
# -----------------------------------------------------------------------------

def snake_case_columns(columns: Iterable[object]) -> list[str]:
    """Convert column labels to snake_case while keeping them readable."""
    out: list[str] = []
    for col in columns:
        s = str(col).strip()
        s = re.sub(r"[\s\-/\.]+", "_", s)
        s = re.sub(r"[()\[\]{}%]+", "", s)
        s = re.sub(r"__+", "_", s)
        out.append(s.strip("_").lower())
    return out


def find_first_existing(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    """Return the first column from candidates that exists in df."""
    for col in candidates:
        if col in df.columns:
            return col
    return None


def clean_id_series(s: pd.Series) -> pd.Series:
    """Standardize cow IDs without accidentally turning missing values into strings."""
    # Convert numbers like 2201.0 to "2201" while preserving non-numeric IDs.
    out = s.copy()
    numeric = pd.to_numeric(out, errors="coerce")
    use_numeric = numeric.notna()
    out = out.astype("string")
    out.loc[use_numeric] = numeric.loc[use_numeric].astype("Int64").astype("string")
    out = out.str.strip()
    out = out.replace({"": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA})
    return out


def clean_group_series(s: pd.Series, group_mapping: Optional[dict] = None) -> pd.Series:
    """Map group labels like G1/pack and convert numeric group labels to nullable int."""
    mapping = DEFAULT_GROUP_MAPPING.copy()
    if group_mapping:
        mapping.update(group_mapping)
    out = s.replace(mapping)
    return pd.to_numeric(out, errors="coerce").astype("Int64")


def parse_date_from_filename(path: Path) -> pd.Timestamp | pd.NaT:
    """Extract YYYYMMDD or YYYY-MM-DD date patterns from a file name."""
    name = path.name
    match = re.search(r"(20\d{2}[01]\d[0-3]\d)", name)
    if match:
        return pd.to_datetime(match.group(1), format="%Y%m%d", errors="coerce")
    match = re.search(r"(20\d{2}[-_][01]?\d[-_][0-3]?\d)", name)
    if match:
        return pd.to_datetime(match.group(1).replace("_", "-"), errors="coerce")
    return pd.NaT


def parse_month_column(month: pd.Series) -> pd.Series:
    """Parse numeric or month-name values into month numbers 1-12."""
    numeric = pd.to_numeric(month, errors="coerce")
    text = month.astype("string").str.strip().str.lower()
    mapped = text.map(MONTH_NAME_TO_NUM)
    parsed = numeric.fillna(mapped)
    return parsed.astype("Int64")


def safe_t_value(n: pd.Series, confidence_level: float) -> pd.Series:
    """Student t critical value, valid only when n >= 2."""
    alpha = 1.0 - confidence_level
    n_num = pd.to_numeric(n, errors="coerce")
    t = pd.Series(np.nan, index=n.index, dtype="float64")
    ok = n_num >= 2
    if ok.any():
        t.loc[ok] = stats.t.ppf(1.0 - alpha / 2.0, n_num.loc[ok] - 1.0)
    return t


def add_t_confidence_interval(
    df: pd.DataFrame,
    mean_col: str,
    se_col: str,
    n_col: str,
    confidence_level: float,
    prefix: str = "",
) -> pd.DataFrame:
    """Add t_value, CI lower, and CI upper columns to a summary table."""
    out = df.copy()
    t_col = f"{prefix}t_value" if prefix else "t_value"
    lo_col = f"{prefix}ci_lower" if prefix else "ci_lower"
    hi_col = f"{prefix}ci_upper" if prefix else "ci_upper"

    out[t_col] = safe_t_value(out[n_col], confidence_level=confidence_level)
    margin = out[t_col] * out[se_col]
    out[lo_col] = out[mean_col] - margin
    out[hi_col] = out[mean_col] + margin
    return out


def write_csv(df: pd.DataFrame, path: Path, round_digits: Optional[int] = None) -> None:
    """Write a CSV, optionally rounding only floating columns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    if round_digits is not None:
        float_cols = out.select_dtypes(include=["float", "float64", "float32"]).columns
        out[float_cols] = out[float_cols].round(round_digits)
    out.to_csv(path, index=False)


def maybe_write_parquet(df: pd.DataFrame, csv_path: Path, write_parquet: bool) -> Optional[Path]:
    """Write a parquet copy next to a CSV if requested."""
    if not write_parquet:
        return None
    parquet_path = csv_path.with_suffix(".parquet")
    df.to_parquet(parquet_path, index=False)
    return parquet_path


def require_columns(df: pd.DataFrame, required: Iterable[str], table_name: str) -> None:
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"{table_name} is missing required columns: {missing}")


# -----------------------------------------------------------------------------
# Afimilk daily data
# -----------------------------------------------------------------------------

def read_afi_parquet_files(afi_dir: Path, recursive: bool = False) -> pd.DataFrame:
    """Read and concatenate Afimilk parquet files, adding source-file metadata."""
    pattern = "**/*.parquet" if recursive else "*.parquet"
    files = sorted(afi_dir.glob(pattern))
    if not files:
        raise FileNotFoundError(f"No parquet files found in {afi_dir} using pattern {pattern!r}")

    frames: list[pd.DataFrame] = []
    for file in files:
        temp = pd.read_parquet(file)
        temp.columns = snake_case_columns(temp.columns)
        temp["source_file"] = file.name
        temp["source_file_date"] = parse_date_from_filename(file)
        frames.append(temp)

    return pd.concat(frames, ignore_index=True)


def standardize_afi_daily(
    raw: pd.DataFrame,
    start_date: Optional[str] = None,
    group_mapping: Optional[dict] = None,
) -> pd.DataFrame:
    """Standardize Afimilk daily records to internal analysis columns."""
    df = raw.copy()
    df.columns = snake_case_columns(df.columns)

    animal_col = find_first_existing(
        df,
        [
            "farm_animal_id",
            "animal_id",
            "cow_id",
            "cowid",
            "cow",
            "id",
            "animal",
        ],
    )
    group_col = find_first_existing(df, ["grp", "group", "pen", "pen_group", "group_id"])
    dim_col = find_first_existing(df, ["dim", "days_in_milk", "days_milk", "dim_dart"])
    date_col = find_first_existing(
        df,
        ["file_date", "date", "day", "observation_date", "milk_date", "event_date"],
    )
    run_dt_col = find_first_existing(
        df,
        ["run_date_time", "run_datetime", "run_date", "run_time", "created_at", "timestamp"],
    )
    daily_yield_col = find_first_existing(
        df,
        ["daily_yield", "yield", "milk_yield", "total_yield", "milk", "daily_milk_yield"],
    )

    if animal_col is None:
        raise ValueError("Could not find an animal ID column in Afimilk parquet files.")
    if group_col is None:
        raise ValueError("Could not find a group column in Afimilk parquet files.")
    if dim_col is None:
        raise ValueError("Could not find a DIM/days-in-milk column in Afimilk parquet files.")

    out = pd.DataFrame()
    out["animal_id"] = clean_id_series(df[animal_col])
    out["group"] = clean_group_series(df[group_col], group_mapping=group_mapping)
    out["dim"] = pd.to_numeric(df[dim_col], errors="coerce")

    if daily_yield_col is not None:
        out["daily_yield"] = pd.to_numeric(df[daily_yield_col], errors="coerce")
    else:
        y1 = find_first_existing(df, ["yield_s_1", "yield_session_1", "yield_1", "milk_s_1"])
        y2 = find_first_existing(df, ["yield_s_2", "yield_session_2", "yield_2", "milk_s_2"])
        if y1 is None or y2 is None:
            raise ValueError(
                "Could not find daily_yield/yield or yield_s_1 + yield_s_2 columns in Afimilk parquet files."
            )
        out["daily_yield"] = pd.to_numeric(df[y1], errors="coerce").fillna(0) + pd.to_numeric(
            df[y2], errors="coerce"
        ).fillna(0)

    if date_col is not None:
        out["day"] = pd.to_datetime(df[date_col], errors="coerce").dt.floor("D")
    else:
        out["day"] = pd.to_datetime(df["source_file_date"], errors="coerce").dt.floor("D")

    if out["day"].isna().all():
        raise ValueError(
            "Could not determine observation dates. Add a file_date/date column or use filenames with YYYYMMDD/YYY-MM-DD."
        )

    if run_dt_col is not None:
        out["run_date_time"] = pd.to_datetime(df[run_dt_col], errors="coerce")
    else:
        out["run_date_time"] = pd.NaT

    out["source_file"] = df.get("source_file", pd.Series(pd.NA, index=df.index))
    out["year"] = out["day"].dt.year.astype("Int64")
    out["month_num"] = out["day"].dt.month.astype("Int64")
    out["month"] = out["day"].dt.month_name()

    # Remove rows without the basic keys needed for analysis.
    out = out.dropna(subset=["animal_id", "group", "day"]).copy()

    if start_date:
        start = pd.to_datetime(start_date)
        out = out[out["day"] >= start].copy()

    out = out.sort_values(["day", "group", "animal_id"]).reset_index(drop=True)
    return out


def make_yield_and_dim_table(afi: pd.DataFrame) -> pd.DataFrame:
    """Create Table 3.1 style cow-level Afimilk daily dataset."""
    return pd.DataFrame(
        {
            "Farm Animal ID": afi["animal_id"],
            "Grp": afi["group"],
            "DIM": afi["dim"],
            "Date": afi["day"].dt.date,
            "Run Date Time": afi["run_date_time"],
            "Yield": afi["daily_yield"],
        }
    )


# -----------------------------------------------------------------------------
# Body weight data
# -----------------------------------------------------------------------------

def load_weights_csv(
    weights_csv: Path,
    afi_years: Iterable[int],
    weight_year: Optional[int] = None,
    group_mapping: Optional[dict] = None,
) -> pd.DataFrame:
    """Load and standardize PCDart/body-weight records."""
    raw = pd.read_csv(weights_csv)
    raw.columns = snake_case_columns(raw.columns)

    animal_col = find_first_existing(
        raw,
        ["farm_animal_id", "cow_id", "cowid", "cow", "animal_id", "animal", "id"],
    )
    weight_col = find_first_existing(raw, ["weight", "body_weight", "bw", "bodyweight"])
    date_col = find_first_existing(raw, ["date", "file_date", "observation_date", "event_date"])
    month_col = find_first_existing(raw, ["month", "month_num", "mo"])
    year_col = find_first_existing(raw, ["year", "yr"])
    group_col = find_first_existing(raw, ["grp", "group", "pen", "pen_group", "group_id"])

    if animal_col is None:
        raise ValueError("Could not find a cow/animal ID column in the weight CSV.")
    if weight_col is None:
        raise ValueError("Could not find a Weight/body-weight column in the weight CSV.")

    out = pd.DataFrame()
    out["animal_id"] = clean_id_series(raw[animal_col])
    out["weight"] = pd.to_numeric(raw[weight_col], errors="coerce")

    if date_col is not None:
        parsed_date = pd.to_datetime(raw[date_col], errors="coerce", format="mixed")
        out["weight_date"] = parsed_date.dt.floor("D")
        out["year"] = parsed_date.dt.year.astype("Int64")
        out["month_num"] = parsed_date.dt.month.astype("Int64")
    else:
        out["weight_date"] = pd.NaT
        if month_col is None:
            raise ValueError("Weight CSV needs either a Date column or a Month column.")
        out["month_num"] = parse_month_column(raw[month_col])

        if year_col is not None:
            out["year"] = pd.to_numeric(raw[year_col], errors="coerce").astype("Int64")
        elif weight_year is not None:
            out["year"] = int(weight_year)
        else:
            years = sorted({int(y) for y in afi_years if pd.notna(y)})
            if len(years) == 1:
                out["year"] = years[0]
            else:
                raise ValueError(
                    "Weight CSV has Month but no Date/Year. Pass --weight-year because Afimilk spans multiple years."
                )

    if group_col is not None:
        out["weight_group"] = clean_group_series(raw[group_col], group_mapping=group_mapping)
    else:
        out["weight_group"] = pd.NA

    out = out.dropna(subset=["animal_id", "weight", "year", "month_num"]).copy()
    out["year"] = out["year"].astype("Int64")
    out["month_num"] = out["month_num"].astype("Int64")
    out["month"] = pd.to_datetime(
        out["year"].astype(str) + "-" + out["month_num"].astype(str) + "-01",
        errors="coerce",
    ).dt.month_name()

    # If multiple records exist per cow-month, average them into one monthly weight.
    # This prevents daily Afimilk rows from over-weighting cows with repeated weight entries.
    grouped = (
        out.groupby(["animal_id", "year", "month_num"], as_index=False)
        .agg(
            weight=("weight", "mean"),
            weight_date=("weight_date", "max"),
            weight_group=("weight_group", lambda x: x.mode(dropna=True).iloc[0] if not x.mode(dropna=True).empty else pd.NA),
        )
        .copy()
    )
    grouped["month"] = pd.to_datetime(
        grouped["year"].astype(str) + "-" + grouped["month_num"].astype(str) + "-01",
        errors="coerce",
    ).dt.month_name()
    return grouped


def cow_month_group_from_afi(afi: pd.DataFrame) -> pd.DataFrame:
    """One row per cow-month with the most frequent Afimilk group and average DIM."""
    require_columns(afi, ["animal_id", "year", "month_num", "group", "dim"], "afi")

    def mode_or_na(x: pd.Series):
        mode = x.mode(dropna=True)
        return mode.iloc[0] if not mode.empty else pd.NA

    out = (
        afi.groupby(["animal_id", "year", "month_num"], as_index=False)
        .agg(
            group=("group", mode_or_na),
            avg_dim=("dim", "mean"),
            first_day=("day", "min"),
            last_day=("day", "max"),
            n_afi_days=("day", "nunique"),
        )
        .copy()
    )
    out["month"] = pd.to_datetime(
        out["year"].astype(str) + "-" + out["month_num"].astype(str) + "-01",
        errors="coerce",
    ).dt.month_name()
    return out


def merge_dim_weights(afi: pd.DataFrame, weights: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Create the daily Table 3.2 dataset and a de-duplicated cow-month weight dataset.

    Returns
    -------
    dim_weights_daily:
        Daily Afimilk DIM rows with monthly weight repeated for display/table 3.2.
    cow_month_weight:
        One row per cow-year-month used for unbiased group-level weight summaries.
    """
    cow_month = cow_month_group_from_afi(afi)
    cow_month_weight = cow_month.merge(
        weights[["animal_id", "year", "month_num", "weight", "weight_date", "weight_group"]],
        on=["animal_id", "year", "month_num"],
        how="inner",
    )

    # Prefer Afimilk group assignment, but fill from the weight file if Afimilk group is missing.
    cow_month_weight["group"] = cow_month_weight["group"].fillna(cow_month_weight["weight_group"])
    cow_month_weight = cow_month_weight.dropna(subset=["group", "weight"]).copy()
    cow_month_weight["group"] = cow_month_weight["group"].astype("Int64")

    dim_weights_daily = afi[["animal_id", "day", "dim", "year", "month_num", "month", "group"]].merge(
        weights[["animal_id", "year", "month_num", "weight"]],
        on=["animal_id", "year", "month_num"],
        how="inner",
    )
    dim_weights_daily = dim_weights_daily.sort_values(["animal_id", "day"]).reset_index(drop=True)

    return dim_weights_daily, cow_month_weight


def make_dim_weights_merged_table(dim_weights_daily: pd.DataFrame) -> pd.DataFrame:
    """Create Table 3.2 style dataset."""
    return pd.DataFrame(
        {
            "Farm Animal ID": dim_weights_daily["animal_id"],
            "File Date": dim_weights_daily["day"].dt.date,
            "DIM": dim_weights_daily["dim"],
            "Weight": dim_weights_daily["weight"],
        }
    )


# -----------------------------------------------------------------------------
# Chapter 3 summaries
# -----------------------------------------------------------------------------

def summarize_yield_group_day(afi: pd.DataFrame, confidence_level: float = 0.80) -> pd.DataFrame:
    """Create Table 3.3: daily group-level yield and DIM summaries."""
    require_columns(afi, ["animal_id", "day", "month", "group", "dim", "daily_yield"], "afi")
    base = afi.dropna(subset=["day", "group", "daily_yield"]).copy()

    grouped = (
        base.groupby(["day", "year", "month_num", "month", "group"], as_index=False)
        .agg(
            avg_dim=("dim", "mean"),
            avg_daily_yield=("daily_yield", "mean"),
            yield_std=("daily_yield", lambda x: x.std(ddof=1)),
            n_animals=("animal_id", "nunique"),
        )
        .copy()
    )
    grouped["se"] = grouped["yield_std"] / np.sqrt(grouped["n_animals"])
    grouped = add_t_confidence_interval(
        grouped,
        mean_col="avg_daily_yield",
        se_col="se",
        n_col="n_animals",
        confidence_level=confidence_level,
    )
    grouped = grouped.sort_values(["day", "group"]).reset_index(drop=True)

    out = grouped.rename(
        columns={
            "day": "Day",
            "year": "Year",
            "month": "Month",
            "month_num": "Month Num",
            "group": "Group",
            "avg_dim": "Avg DIM",
            "avg_daily_yield": "Avg Daily Yield",
            "yield_std": "Yield STD",
            "n_animals": "N Animals",
            "se": "SE",
            "t_value": "T Value",
            "ci_lower": "CI Lower",
            "ci_upper": "CI Upper",
        }
    )
    return out[
        [
            "Day",
            "Year",
            "Month",
            "Month Num",
            "Group",
            "Avg DIM",
            "Avg Daily Yield",
            "Yield STD",
            "N Animals",
            "SE",
            "T Value",
            "CI Lower",
            "CI Upper",
        ]
    ]


def summarize_animal_month_yield(afi: pd.DataFrame) -> pd.DataFrame:
    """First stage for Table 3.4: mean daily yield per cow, group, and month."""
    base = afi.dropna(subset=["animal_id", "group", "day", "daily_yield"]).copy()
    out = (
        base.groupby(["animal_id", "year", "month_num", "month", "group"], as_index=False)
        .agg(
            avg_monthly_yield=("daily_yield", "mean"),
            n_days=("day", "nunique"),
            avg_dim=("dim", "mean"),
            first_day=("day", "min"),
            last_day=("day", "max"),
        )
        .sort_values(["year", "month_num", "group", "animal_id"])
        .reset_index(drop=True)
    )
    return out.rename(
        columns={
            "animal_id": "Farm Animal ID",
            "year": "Year",
            "month_num": "Month Num",
            "month": "Month",
            "group": "Group",
            "avg_monthly_yield": "Avg Monthly Yield",
            "n_days": "N Days",
            "avg_dim": "Avg DIM",
            "first_day": "First Day",
            "last_day": "Last Day",
        }
    )


def summarize_yield_group_month(animal_month_yield: pd.DataFrame, confidence_level: float = 0.80) -> pd.DataFrame:
    """Create Table 3.4: monthly group-level milk-yield summaries."""
    require_columns(
        animal_month_yield,
        ["Farm Animal ID", "Year", "Month Num", "Month", "Group", "Avg Monthly Yield"],
        "animal_month_yield",
    )
    base = animal_month_yield.dropna(subset=["Avg Monthly Yield", "Group"]).copy()
    grouped = (
        base.groupby(["Year", "Month Num", "Month", "Group"], as_index=False)
        .agg(
            avg_monthly_yield=("Avg Monthly Yield", "mean"),
            yield_std=("Avg Monthly Yield", lambda x: x.std(ddof=1)),
            n_animals=("Farm Animal ID", "nunique"),
        )
        .copy()
    )
    grouped["se"] = grouped["yield_std"] / np.sqrt(grouped["n_animals"])
    grouped = add_t_confidence_interval(
        grouped,
        mean_col="avg_monthly_yield",
        se_col="se",
        n_col="n_animals",
        confidence_level=confidence_level,
    )
    grouped = grouped.sort_values(["Year", "Month Num", "Group"]).reset_index(drop=True)
    out = grouped.rename(
        columns={
            "avg_monthly_yield": "Avg Monthly Yield",
            "yield_std": "Yield STD",
            "n_animals": "N Animals",
            "se": "SE",
            "t_value": "T Value",
            "ci_lower": "CI Lower",
            "ci_upper": "CI Upper",
        }
    )
    return out[
        [
            "Year",
            "Month Num",
            "Month",
            "Group",
            "Avg Monthly Yield",
            "Yield STD",
            "N Animals",
            "SE",
            "T Value",
            "CI Lower",
            "CI Upper",
        ]
    ]


def summarize_weight_group(cow_month_weight: pd.DataFrame) -> pd.DataFrame:
    """Create Table 3.5: monthly group-level body-weight summaries."""
    require_columns(cow_month_weight, ["animal_id", "year", "month_num", "month", "group", "weight"], "cow_month_weight")
    base = cow_month_weight.dropna(subset=["group", "weight"]).copy()
    # Ensure one record per cow-year-month-group-weight to avoid daily duplication.
    base = base.drop_duplicates(subset=["animal_id", "year", "month_num", "group"])
    grouped = (
        base.groupby(["year", "month_num", "month", "group"], as_index=False)
        .agg(
            n_animals=("animal_id", "nunique"),
            avg_weight=("weight", "mean"),
            weight_std=("weight", lambda x: x.std(ddof=1)),
        )
        .copy()
    )
    grouped["se"] = grouped["weight_std"] / np.sqrt(grouped["n_animals"])
    grouped = grouped.sort_values(["year", "month_num", "group"]).reset_index(drop=True)
    out = grouped.rename(
        columns={
            "year": "Year",
            "month_num": "Month Num",
            "month": "Month",
            "group": "Group",
            "n_animals": "N Animals",
            "avg_weight": "Avg Weight",
            "weight_std": "Weight STD",
            "se": "SE",
        }
    )
    return out[["Year", "Month Num", "Month", "Group", "N Animals", "Avg Weight", "Weight STD", "SE"]]


def summarize_per_cow_daily_yield(afi: pd.DataFrame, confidence_level: float = 0.80) -> pd.DataFrame:
    """Create individual cow-level daily milk-yield summaries with t-CIs."""
    base = afi.dropna(subset=["animal_id", "day", "daily_yield"]).copy()

    # Collapse duplicate cow-day records if present.
    daily = (
        base.groupby(["animal_id", "day"], as_index=False)
        .agg(
            daily_yield=("daily_yield", "mean"),
            dim=("dim", "mean"),
            group=("group", lambda x: x.mode(dropna=True).iloc[0] if not x.mode(dropna=True).empty else pd.NA),
        )
        .copy()
    )

    def mode_or_na(x: pd.Series):
        mode = x.mode(dropna=True)
        return mode.iloc[0] if not mode.empty else pd.NA

    out = (
        daily.groupby("animal_id", as_index=False)
        .agg(
            group=("group", mode_or_na),
            avg_dim=("dim", "mean"),
            n_days=("day", "nunique"),
            avg_daily_yield=("daily_yield", "mean"),
            yield_std=("daily_yield", lambda x: x.std(ddof=1)),
            first_day=("day", "min"),
            last_day=("day", "max"),
        )
        .copy()
    )
    out["se"] = out["yield_std"] / np.sqrt(out["n_days"])
    out = add_t_confidence_interval(
        out,
        mean_col="avg_daily_yield",
        se_col="se",
        n_col="n_days",
        confidence_level=confidence_level,
    )
    out = out.sort_values(["group", "animal_id"]).reset_index(drop=True)
    out = out.rename(
        columns={
            "animal_id": "Farm Animal ID",
            "group": "Group",
            "avg_dim": "Avg DIM",
            "n_days": "N Days",
            "avg_daily_yield": "Avg Daily Yield",
            "yield_std": "Yield STD",
            "se": "SE",
            "t_value": "T Value",
            "ci_lower": "CI Lower",
            "ci_upper": "CI Upper",
            "first_day": "First Day",
            "last_day": "Last Day",
        }
    )
    return out[
        [
            "Farm Animal ID",
            "Group",
            "Avg DIM",
            "N Days",
            "Avg Daily Yield",
            "Yield STD",
            "SE",
            "T Value",
            "CI Lower",
            "CI Upper",
            "First Day",
            "Last Day",
        ]
    ]


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------

def run_pipeline(
    afi_dir: Path("# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------

def run_pipeline(
    afi_dir: Path = Path("/data/afimilk-daily"),
    output_dir: Path = Path("/outputs/chapter3"),
    weights_csv: Optional[Path] = Path("/data/allWeights.csv"),
    start_date: Optional[str] = "2024-05-01",
    confidence_level: float = 0.80,
    weight_year: Optional[int] = None,
    recursive: bool = False,
    write_parquet: bool = False,
) -> PipelineOutputs:
    """Run the complete Chapter 3 pipeline and write CSV outputs."""
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_afi = read_afi_parquet_files(afi_dir, recursive=recursive)
    afi = standardize_afi_daily(raw_afi, start_date=start_date)

    if afi.empty:
        raise ValueError("No Afimilk rows remain after cleaning/filtering. Check --start-date and source files.")

    # Table 3.1
    table_3_1 = make_yield_and_dim_table(afi)
    path_3_1 = output_dir / "table_3_1_yield_and_dim.csv"
    write_csv(table_3_1, path_3_1)
    maybe_write_parquet(table_3_1, path_3_1, write_parquet)

    # Table 3.3
    table_3_3 = summarize_yield_group_day(afi, confidence_level=confidence_level)
    path_3_3 = output_dir / "table_3_3_yield_group_day.csv"
    write_csv(table_3_3, path_3_3)
    maybe_write_parquet(table_3_3, path_3_3, write_parquet)

    # Animal-month intermediate and Table 3.4
    animal_month_yield = summarize_animal_month_yield(afi)
    path_animal_month = output_dir / "animal_month_yield.csv"
    write_csv(animal_month_yield, path_animal_month)
    maybe_write_parquet(animal_month_yield, path_animal_month, write_parquet)

    table_3_4 = summarize_yield_group_month(animal_month_yield, confidence_level=confidence_level)
    path_3_4 = output_dir / "table_3_4_yield_group_month.csv"
    write_csv(table_3_4, path_3_4)
    maybe_write_parquet(table_3_4, path_3_4, write_parquet)

    # Per-cow summary
    per_cow = summarize_per_cow_daily_yield(afi, confidence_level=confidence_level)
    path_per_cow = output_dir / "per_cow_daily_yield_summary.csv"
    write_csv(per_cow, path_per_cow)
    maybe_write_parquet(per_cow, path_per_cow, write_parquet)

    path_3_2: Optional[Path] = None
    path_3_5: Optional[Path] = None
    path_cow_month_weight: Optional[Path] = None
    weights_summary: dict[str, object] = {"weights_csv_used": False}

    if weights_csv is not None:
        weights = load_weights_csv(
            weights_csv,
            afi_years=afi["year"].dropna().unique(),
            weight_year=weight_year,
        )
        dim_weights_daily, cow_month_weight = merge_dim_weights(afi, weights)

        table_3_2 = make_dim_weights_merged_table(dim_weights_daily)
        path_3_2 = output_dir / "table_3_2_dim_weights_merged.csv"
        write_csv(table_3_2, path_3_2)
        maybe_write_parquet(table_3_2, path_3_2, write_parquet)

        cow_month_weight_out = cow_month_weight.rename(
            columns={
                "animal_id": "Farm Animal ID",
                "year": "Year",
                "month_num": "Month Num",
                "month": "Month",
                "group": "Group",
                "avg_dim": "Avg DIM",
                "weight": "Weight",
                "weight_date": "Weight Date",
                "n_afi_days": "N AFI Days",
                "first_day": "First AFI Day",
                "last_day": "Last AFI Day",
            }
        )
        path_cow_month_weight = output_dir / "cow_month_weight.csv"
        write_csv(cow_month_weight_out, path_cow_month_weight)
        maybe_write_parquet(cow_month_weight_out, path_cow_month_weight, write_parquet)

        table_3_5 = summarize_weight_group(cow_month_weight)
        path_3_5 = output_dir / "table_3_5_weight_group.csv"
        write_csv(table_3_5, path_3_5)
        maybe_write_parquet(table_3_5, path_3_5, write_parquet)

        weights_summary = {
            "weights_csv_used": True,
            "weights_csv": str(weights_csv),
            "weight_rows_after_cleaning": int(len(weights)),
            "dim_weight_daily_rows": int(len(dim_weights_daily)),
            "cow_month_weight_rows": int(len(cow_month_weight)),
            "weight_group_rows": int(len(table_3_5)),
        }

    run_summary = {
        "afi_dir": str(afi_dir),
        "output_dir": str(output_dir),
        "start_date": start_date,
        "confidence_level": confidence_level,
        "raw_afi_rows": int(len(raw_afi)),
        "clean_afi_rows": int(len(afi)),
        "afi_start_day": str(afi["day"].min().date()),
        "afi_end_day": str(afi["day"].max().date()),
        "unique_animals": int(afi["animal_id"].nunique()),
        "unique_groups": [int(x) for x in sorted(afi["group"].dropna().unique())],
        "yield_and_dim_rows": int(len(table_3_1)),
        "yield_group_day_rows": int(len(table_3_3)),
        "animal_month_yield_rows": int(len(animal_month_yield)),
        "yield_group_month_rows": int(len(table_3_4)),
        "per_cow_rows": int(len(per_cow)),
        **weights_summary,
        "output_files": {
            "table_3_1_yield_and_dim": str(path_3_1),
            "table_3_2_dim_weights_merged": str(path_3_2) if path_3_2 else None,
            "table_3_3_yield_group_day": str(path_3_3),
            "table_3_4_yield_group_month": str(path_3_4),
            "table_3_5_weight_group": str(path_3_5) if path_3_5 else None,
            "per_cow_daily_yield_summary": str(path_per_cow),
            "animal_month_yield": str(path_animal_month),
            "cow_month_weight": str(path_cow_month_weight) if path_cow_month_weight else None,
        },
    }
    path_summary = output_dir / "run_summary.json"
    path_summary.write_text(json.dumps(run_summary, indent=2), encoding="utf-8")

    return PipelineOutputs(
        output_dir=output_dir,
        yield_and_dim=path_3_1,
        dim_weights_merged=path_3_2,
        yield_group_day=path_3_3,
        yield_group_month=path_3_4,
        weight_group=path_3_5,
        per_cow_daily_yield_summary=path_per_cow,
        animal_month_yield=path_animal_month,
        cow_month_weight=path_cow_month_weight,
        run_summary=path_summary,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create Chapter 3 herd-characteristics tables from Afimilk parquet files and optional PCDart weights CSV."
    )
    parser.add_argument("--afi-dir", required=True, type=Path, help="Directory containing Afimilk daily parquet files.")
    parser.add_argument("--weights-csv", type=Path, default=None, help="Optional PCDart/body-weight CSV file.")
    parser.add_argument("--output-dir", type=Path, default=Path("chapter3_outputs"), help="Output directory for CSV files.")
    parser.add_argument("--start-date", default=None, help="Optional first observation date to keep, e.g. 2025-06-01.")
    parser.add_argument("--confidence", type=float, default=0.80, help="Confidence level for milk-yield intervals. Default: 0.80.")
    parser.add_argument(
        "--weight-year",
        type=int,
        default=None,
        help="Year to assign to weight CSV rows when the CSV has Month but no Date/Year column.",
    )
    parser.add_argument("--recursive", action="store_true", help="Read parquet files recursively under --afi-dir.")
    parser.add_argument("--write-parquet", action="store_true", help="Also write parquet copies of each output table.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not (0 < args.confidence < 1):
        parser.error("--confidence must be between 0 and 1, e.g. 0.80")

    try:
        outputs = run_pipeline(
            afi_dir=args.afi_dir,
            weights_csv=args.weights_csv,
            output_dir=args.output_dir,
            start_date=args.start_date,
            confidence_level=args.confidence,
            weight_year=args.weight_year,
            recursive=args.recursive,
            write_parquet=args.write_parquet,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print a readable error.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Chapter 3 pipeline complete. Files written:")
    for label, path in outputs.__dict__.items():
        if label == "output_dir" or path is None:
            continue
        print(f"  - {label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"),
    output_dir: Path(""),
    weights_csv: Optional[Path] = None,
    start_date: Optional[str] = None,
    confidence_level: float = 0.80,
    weight_year: Optional[int] = None,
    recursive: bool = False,
    write_parquet: bool = False,
) -> PipelineOutputs:
    """Run the complete Chapter 3 pipeline and write CSV outputs."""
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_afi = read_afi_parquet_files(afi_dir, recursive=recursive)
    afi = standardize_afi_daily(raw_afi, start_date=start_date)

    if afi.empty:
        raise ValueError("No Afimilk rows remain after cleaning/filtering. Check --start-date and source files.")

    # Table 3.1
    table_3_1 = make_yield_and_dim_table(afi)
    path_3_1 = output_dir / "table_3_1_yield_and_dim.csv"
    write_csv(table_3_1, path_3_1)
    maybe_write_parquet(table_3_1, path_3_1, write_parquet)

    # Table 3.3
    table_3_3 = summarize_yield_group_day(afi, confidence_level=confidence_level)
    path_3_3 = output_dir / "table_3_3_yield_group_day.csv"
    write_csv(table_3_3, path_3_3)
    maybe_write_parquet(table_3_3, path_3_3, write_parquet)

    # Animal-month intermediate and Table 3.4
    animal_month_yield = summarize_animal_month_yield(afi)
    path_animal_month = output_dir / "animal_month_yield.csv"
    write_csv(animal_month_yield, path_animal_month)
    maybe_write_parquet(animal_month_yield, path_animal_month, write_parquet)

    table_3_4 = summarize_yield_group_month(animal_month_yield, confidence_level=confidence_level)
    path_3_4 = output_dir / "table_3_4_yield_group_month.csv"
    write_csv(table_3_4, path_3_4)
    maybe_write_parquet(table_3_4, path_3_4, write_parquet)

    # Per-cow summary
    per_cow = summarize_per_cow_daily_yield(afi, confidence_level=confidence_level)
    path_per_cow = output_dir / "per_cow_daily_yield_summary.csv"
    write_csv(per_cow, path_per_cow)
    maybe_write_parquet(per_cow, path_per_cow, write_parquet)

    path_3_2: Optional[Path] = None
    path_3_5: Optional[Path] = None
    path_cow_month_weight: Optional[Path] = None
    weights_summary: dict[str, object] = {"weights_csv_used": False}

    if weights_csv is not None:
        weights = load_weights_csv(
            weights_csv,
            afi_years=afi["year"].dropna().unique(),
            weight_year=weight_year,
        )
        dim_weights_daily, cow_month_weight = merge_dim_weights(afi, weights)

        table_3_2 = make_dim_weights_merged_table(dim_weights_daily)
        path_3_2 = output_dir / "table_3_2_dim_weights_merged.csv"
        write_csv(table_3_2, path_3_2)
        maybe_write_parquet(table_3_2, path_3_2, write_parquet)

        cow_month_weight_out = cow_month_weight.rename(
            columns={
                "animal_id": "Farm Animal ID",
                "year": "Year",
                "month_num": "Month Num",
                "month": "Month",
                "group": "Group",
                "avg_dim": "Avg DIM",
                "weight": "Weight",
                "weight_date": "Weight Date",
                "n_afi_days": "N AFI Days",
                "first_day": "First AFI Day",
                "last_day": "Last AFI Day",
            }
        )
        path_cow_month_weight = output_dir / "cow_month_weight.csv"
        write_csv(cow_month_weight_out, path_cow_month_weight)
        maybe_write_parquet(cow_month_weight_out, path_cow_month_weight, write_parquet)

        table_3_5 = summarize_weight_group(cow_month_weight)
        path_3_5 = output_dir / "table_3_5_weight_group.csv"
        write_csv(table_3_5, path_3_5)
        maybe_write_parquet(table_3_5, path_3_5, write_parquet)

        weights_summary = {
            "weights_csv_used": True,
            "weights_csv": str(weights_csv),
            "weight_rows_after_cleaning": int(len(weights)),
            "dim_weight_daily_rows": int(len(dim_weights_daily)),
            "cow_month_weight_rows": int(len(cow_month_weight)),
            "weight_group_rows": int(len(table_3_5)),
        }

    run_summary = {
        "afi_dir": str(afi_dir),
        "output_dir": str(output_dir),
        "start_date": start_date,
        "confidence_level": confidence_level,
        "raw_afi_rows": int(len(raw_afi)),
        "clean_afi_rows": int(len(afi)),
        "afi_start_day": str(afi["day"].min().date()),
        "afi_end_day": str(afi["day"].max().date()),
        "unique_animals": int(afi["animal_id"].nunique()),
        "unique_groups": [int(x) for x in sorted(afi["group"].dropna().unique())],
        "yield_and_dim_rows": int(len(table_3_1)),
        "yield_group_day_rows": int(len(table_3_3)),
        "animal_month_yield_rows": int(len(animal_month_yield)),
        "yield_group_month_rows": int(len(table_3_4)),
        "per_cow_rows": int(len(per_cow)),
        **weights_summary,
        "output_files": {
            "table_3_1_yield_and_dim": str(path_3_1),
            "table_3_2_dim_weights_merged": str(path_3_2) if path_3_2 else None,
            "table_3_3_yield_group_day": str(path_3_3),
            "table_3_4_yield_group_month": str(path_3_4),
            "table_3_5_weight_group": str(path_3_5) if path_3_5 else None,
            "per_cow_daily_yield_summary": str(path_per_cow),
            "animal_month_yield": str(path_animal_month),
            "cow_month_weight": str(path_cow_month_weight) if path_cow_month_weight else None,
        },
    }
    path_summary = output_dir / "run_summary.json"
    path_summary.write_text(json.dumps(run_summary, indent=2), encoding="utf-8")

    return PipelineOutputs(
        output_dir=output_dir,
        yield_and_dim=path_3_1,
        dim_weights_merged=path_3_2,
        yield_group_day=path_3_3,
        yield_group_month=path_3_4,
        weight_group=path_3_5,
        per_cow_daily_yield_summary=path_per_cow,
        animal_month_yield=path_animal_month,
        cow_month_weight=path_cow_month_weight,
        run_summary=path_summary,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create Chapter 3 herd-characteristics tables from Afimilk parquet files and optional PCDart weights CSV."
    )
    parser.add_argument("--afi-dir", required=True, type=Path, help="Directory containing Afimilk daily parquet files.")
    parser.add_argument("--weights-csv", type=Path, default=None, help="Optional PCDart/body-weight CSV file.")
    parser.add_argument("--output-dir", type=Path, default=Path("chapter3_outputs"), help="Output directory for CSV files.")
    parser.add_argument("--start-date", default=None, help="Optional first observation date to keep, e.g. 2025-06-01.")
    parser.add_argument("--confidence", type=float, default=0.80, help="Confidence level for milk-yield intervals. Default: 0.80.")
    parser.add_argument(
        "--weight-year",
        type=int,
        default=None,
        help="Year to assign to weight CSV rows when the CSV has Month but no Date/Year column.",
    )
    parser.add_argument("--recursive", action="store_true", help="Read parquet files recursively under --afi-dir.")
    parser.add_argument("--write-parquet", action="store_true", help="Also write parquet copies of each output table.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not (0 < args.confidence < 1):
        parser.error("--confidence must be between 0 and 1, e.g. 0.80")

    try:
        outputs = run_pipeline(
            afi_dir=args.afi_dir,
            weights_csv=args.weights_csv,
            output_dir=args.output_dir,
            start_date=args.start_date,
            confidence_level=args.confidence,
            weight_year=args.weight_year,
            recursive=args.recursive,
            write_parquet=args.write_parquet,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print a readable error.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Chapter 3 pipeline complete. Files written:")
    for label, path in outputs.__dict__.items():
        if label == "output_dir" or path is None:
            continue
        print(f"  - {label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
