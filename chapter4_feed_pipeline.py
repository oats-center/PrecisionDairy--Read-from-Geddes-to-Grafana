#!/usr/bin/env python3
"""
Chapter 4 Feed Traits pipeline.

This module converts notebook-style feed-traits calculations into a reusable,
container-ready Python pipeline. It reads TMR Tracker feed-intake records and a
nutrient table, computes feeding-accuracy outputs, writes CSV backup files, and
can optionally write the outputs to PostgreSQL for Grafana/dashboard use.

Default container paths:
    Feed intake files:   /data/feed-intake
    Nutrient table:      /data/nutrient-table.csv
    CSV outputs:         /outputs/chapter4

Expected PostgreSQL environment variables, when --write-postgres is used:
    POSTGRES_HOST
    POSTGRES_PORT
    POSTGRES_DB
    POSTGRES_USER
    POSTGRES_PASSWORD
    POSTGRES_SCHEMA
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional
from urllib.parse import quote_plus

import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)

DEFAULT_FEED_INTAKE_PATH = Path("/data/feed-intake")
DEFAULT_NUTRIENT_TABLE_PATH = Path("/data/nutrient-table.csv")
DEFAULT_OUTPUT_DIR = Path("/outputs/chapter4")

POSTGRES_TABLE_NAMES: Dict[str, str] = {
    "filtered_feed_intake": "chapter4_filtered_feed_intake",
    "dry_weight_participation": "chapter4_dry_weight_participation",
    "weight_difference_ing": "chapter4_weight_difference_ing",
    "error_by_weight": "chapter4_error_by_weight",
    "nutrient_delivery_by_ingredient": "chapter4_nutrient_delivery_by_ingredient",
    "nutrient_error_by_formula": "chapter4_nutrient_error_by_formula",
}

# Ordered phrase rules are applied before the generic first-three-character rule.
# Keep this mapping small and transparent; add farm-specific changes through
# --ingredient-map rather than editing code for every deployment.
DEFAULT_INGREDIENT_CODE_RULES: tuple[tuple[str, str], ...] = (
    ("CORN SILAGE", "CSL"),
    ("HAYLAGE", "HLG"),
    ("TRITICALE", "TCL"),
    ("SOYBEAN MEAL", "SBM"),
    ("SOY MEAL", "SBM"),
    ("SOY HULL", "SOY"),
    ("SOYHULL", "SOY"),
    ("MOLASSES", "MOL"),
    ("SUPER MIX", "SMX"),
    ("SUPERMIX", "SMX"),
    ("PROTEIN MIX", "PRO"),
    ("DRY COW SUPPLEMENT", "LAC"),
    ("DRYCOW SUPPLEMENT", "LAC"),
    ("DRY COW MIX", "DCW"),
    ("DRYCOW MIX", "DCW"),
)

DEFAULT_CODE_ALIASES: Mapping[str, str] = {
    "CSL": "CSL",
    "HLG": "HLG",
    "TCL": "TCL",
    "TRI": "TCL",
    "SBM": "SBM",
    "SOY": "SOY",
    "MOL": "MOL",
    "SMX": "SMX",
    "SUP": "SMX",
    "PRO": "PRO",
    "LAC": "LAC",
    "DCW": "DCW",
}

FEED_COLUMN_CANDIDATES: Mapping[str, tuple[str, ...]] = {
    "feeding_date": (
        "feeding_date_std",
        "feeding date std",
        "feeding_date",
        "feeding date",
        "feed date",
        "fed date",
        "date fed",
        "date",
    ),
    "recipe_display_name": (
        "recipe_display_name",
        "recipe display name",
        "recipe_name",
        "recipe name",
        "ration name",
        "formula name",
        "recipe",
        "formula",
        "ration",
    ),
    "ingredient_name": (
        "ingredient_name",
        "ingredient name",
        "feed ingredient",
        "ingredient",
        "feedstuff",
    ),
    "pen_name": (
        "pen_name",
        "pen name",
        "group_name",
        "group name",
        "group",
        "pen",
    ),
    "dry_call_weight": (
        "dry_call_weight",
        "dry call weight",
        "dry_called_weight",
        "dry called weight",
        "call dry weight",
        "called dry weight",
        "dry target weight",
        "target dry weight",
        "dry matter call weight",
        "dry matter called weight",
        "called weight dm",
    ),
    "dry_actual_weight": (
        "dry_actual_weight",
        "dry actual weight",
        "actual dry weight",
        "dry delivered weight",
        "delivered dry weight",
        "actual weight dm",
        "dry matter actual weight",
        "dry matter delivered weight",
    ),
}

NUTRIENT_COLUMN_CANDIDATES: Mapping[str, tuple[str, ...]] = {
    "ingredient_code": (
        "ingredient_code",
        "ingredient code",
        "code",
        "feed code",
        "ingredient id",
    ),
    "ingredient_name": (
        "ingredient",
        "ingredient_name",
        "ingredient name",
        "feed ingredient",
        "feedstuff",
    ),
    "protein_pct": (
        "crude_protein_pct",
        "crude protein pct",
        "crude protein (%)",
        "crude protein",
        "protein_pct",
        "protein (%)",
        "protein",
        "cp",
        "cp_pct",
    ),
    "andfom_pct": (
        "andfom_pct",
        "andfom (%)",
        "andfom",
        "ndf_pct",
        "ndf (%)",
        "ndf",
        "neutral detergent fiber",
        "neutral detergent fiber (%)",
    ),
    "starch_pct": (
        "starch_pct",
        "starch (%)",
        "starch",
    ),
    "nutrient_date": (
        "date",
        "sample date",
        "nutrient date",
        "analysis date",
    ),
}

SUPPORTED_INPUT_SUFFIXES = {".csv", ".txt", ".tsv", ".parquet", ".pq", ".xlsx", ".xls"}


def normalize_name(value: Any) -> str:
    """Normalize names for robust column matching."""
    text = str(value).strip().lower()
    text = re.sub(r"[%()\[\]{}]+", " ", text)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def clean_text(value: Any) -> Optional[str]:
    """Return a stripped string or None for empty/missing values."""
    if pd.isna(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "na", "n/a", "--", "—"}:
        return None
    return text


def clean_numeric(series: pd.Series) -> pd.Series:
    """Convert a messy numeric column to float."""
    return pd.to_numeric(
        series.astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("%", "", regex=False)
        .str.replace("—", "", regex=False)
        .str.replace("--", "", regex=False)
        .str.strip(),
        errors="coerce",
    )


def clean_date(series: pd.Series) -> pd.Series:
    """Parse dates, including normal date strings and Excel serial date numbers."""
    parsed = pd.to_datetime(series, errors="coerce")

    # Try Excel serial dates only for values that did not parse and look numeric.
    missing_mask = parsed.isna()
    numeric_values = pd.to_numeric(series.where(missing_mask), errors="coerce")
    excel_mask = missing_mask & numeric_values.notna() & numeric_values.between(20000, 80000)
    if excel_mask.any():
        parsed.loc[excel_mask] = pd.to_datetime(
            numeric_values.loc[excel_mask], unit="D", origin="1899-12-30", errors="coerce"
        )
    return parsed.dt.date.astype("object")


def safe_percent(numerator: pd.Series | np.ndarray, denominator: pd.Series | np.ndarray) -> np.ndarray:
    """Compute numerator / denominator * 100 with NaN when denominator is zero/missing."""
    numerator_array = np.asarray(numerator, dtype="float64")
    denominator_array = np.asarray(denominator, dtype="float64")
    return np.where(
        np.isfinite(denominator_array) & (denominator_array != 0),
        numerator_array / denominator_array * 100.0,
        np.nan,
    )


def find_column(
    df: pd.DataFrame,
    candidates: Iterable[str],
    *,
    required: bool = True,
    description: str = "column",
) -> Optional[str]:
    """Find a column using exact normalized matching, then token containment."""
    normalized_to_original: Dict[str, str] = {normalize_name(col): col for col in df.columns}
    normalized_columns = list(normalized_to_original.keys())

    for candidate in candidates:
        key = normalize_name(candidate)
        if key in normalized_to_original:
            return normalized_to_original[key]

    for candidate in candidates:
        key = normalize_name(candidate)
        compact_key = key.replace("_", "")
        for normalized_col in normalized_columns:
            if normalized_col.replace("_", "") == compact_key:
                return normalized_to_original[normalized_col]

    for candidate in candidates:
        tokens = [token for token in normalize_name(candidate).split("_") if token]
        if not tokens:
            continue
        for normalized_col in normalized_columns:
            if all(token in normalized_col.split("_") or token in normalized_col for token in tokens):
                # Avoid accidentally selecting run_date when looking for feeding date.
                if "feeding" in tokens or "feed" in tokens:
                    if "run" in normalized_col:
                        continue
                return normalized_to_original[normalized_col]

    if required:
        raise ValueError(
            f"Could not find required {description}. Available columns: {list(df.columns)}"
        )
    return None


def read_table_file(path: Path) -> pd.DataFrame:
    """Read one CSV/TXT/Parquet/Excel table into a DataFrame."""
    suffix = path.suffix.lower()
    LOGGER.info("Reading %s", path)

    if suffix in {".parquet", ".pq"}:
        df = pd.read_parquet(path)
        df["source_file"] = path.name
        return df

    if suffix == ".csv":
        df = pd.read_csv(path, low_memory=False)
        df["source_file"] = path.name
        return df

    if suffix in {".txt", ".tsv"}:
        sep = "\t" if suffix == ".tsv" else None
        df = pd.read_csv(path, sep=sep, engine="python", low_memory=False)
        df["source_file"] = path.name
        return df

    if suffix in {".xlsx", ".xls"}:
        sheets = pd.read_excel(path, sheet_name=None)
        frames: list[pd.DataFrame] = []
        for sheet_name, sheet_df in sheets.items():
            if sheet_df.empty:
                continue
            sheet_df = sheet_df.copy()
            sheet_df["source_file"] = path.name
            sheet_df["source_sheet"] = sheet_name
            frames.append(sheet_df)
        if not frames:
            raise ValueError(f"Excel file has no non-empty sheets: {path}")
        return pd.concat(frames, ignore_index=True)

    raise ValueError(f"Unsupported input file type: {path}")


def collect_input_files(path: Path) -> list[Path]:
    """Collect supported feed-intake files from a file or directory."""
    if path.is_file():
        if path.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
            raise ValueError(f"Unsupported file extension for {path}")
        return [path]

    if not path.exists():
        raise FileNotFoundError(f"Input path does not exist: {path}")

    files = [
        file
        for file in sorted(path.rglob("*"))
        if file.is_file()
        and not file.name.startswith(".")
        and file.suffix.lower() in SUPPORTED_INPUT_SUFFIXES
    ]
    if not files:
        raise FileNotFoundError(f"No supported input files found in {path}")
    return files


def load_feed_intake_data(feed_intake_path: str | Path) -> pd.DataFrame:
    """Load and concatenate feed-intake records from one file or a directory."""
    input_path = Path(feed_intake_path)
    files = collect_input_files(input_path)
    frames = [read_table_file(file) for file in files]
    combined = pd.concat(frames, ignore_index=True)
    LOGGER.info("Loaded %s rows from %s feed-intake file(s).", len(combined), len(files))
    return combined


def load_mapping_file(mapping_path: Optional[str | Path]) -> dict[str, str]:
    """Load optional ingredient-name-to-code mapping from CSV/Excel/Parquet."""
    if mapping_path is None:
        return {}

    mapping_df = read_table_file(Path(mapping_path))
    raw_col = find_column(
        mapping_df,
        ("raw", "raw ingredient", "ingredient", "ingredient name", "ingredient_name"),
        description="ingredient mapping raw/name column",
    )
    code_col = find_column(
        mapping_df,
        ("code", "ingredient code", "ingredient_code"),
        description="ingredient mapping code column",
    )

    mapping: dict[str, str] = {}
    for raw_value, code_value in zip(mapping_df[raw_col], mapping_df[code_col]):
        raw_text = clean_text(raw_value)
        code_text = clean_text(code_value)
        if raw_text and code_text:
            mapping[raw_text.upper()] = code_text.upper()[:10]
    LOGGER.info("Loaded %s ingredient mapping rules from %s.", len(mapping), mapping_path)
    return mapping


def standardize_ingredient_code(value: Any, extra_mapping: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Create the ingredient code used to join TMR records to the nutrient table."""
    text = clean_text(value)
    if text is None:
        return None

    text_upper = re.sub(r"\s+", " ", text.upper()).strip()
    compact = re.sub(r"[^A-Z0-9]", "", text_upper)

    if extra_mapping:
        if text_upper in extra_mapping:
            return extra_mapping[text_upper]
        if compact in extra_mapping:
            return extra_mapping[compact]

    if compact in DEFAULT_CODE_ALIASES:
        return DEFAULT_CODE_ALIASES[compact]

    first_three = compact[:3]
    if first_three in DEFAULT_CODE_ALIASES:
        return DEFAULT_CODE_ALIASES[first_three]

    for phrase, code in DEFAULT_INGREDIENT_CODE_RULES:
        phrase_compact = re.sub(r"[^A-Z0-9]", "", phrase)
        if phrase in text_upper or phrase_compact in compact:
            return code

    return first_three if first_three else None


def prepare_feed_intake_data(
    raw_feed: pd.DataFrame,
    *,
    ingredient_mapping: Optional[Mapping[str, str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    group_by_pen: bool = False,
) -> pd.DataFrame:
    """
    Standardize the feed-intake dataset and compute formula dry-weight totals.

    Retained fields follow the Chapter 4 definition: feeding date, recipe name,
    ingredient name, pen name if present, dry called weight, and dry actual weight.
    """
    date_col = find_column(raw_feed, FEED_COLUMN_CANDIDATES["feeding_date"], description="feeding date")
    recipe_col = find_column(raw_feed, FEED_COLUMN_CANDIDATES["recipe_display_name"], description="recipe name")
    ingredient_col = find_column(raw_feed, FEED_COLUMN_CANDIDATES["ingredient_name"], description="ingredient name")
    call_col = find_column(raw_feed, FEED_COLUMN_CANDIDATES["dry_call_weight"], description="dry call weight")
    actual_col = find_column(raw_feed, FEED_COLUMN_CANDIDATES["dry_actual_weight"], description="dry actual weight")
    pen_col = find_column(
        raw_feed,
        FEED_COLUMN_CANDIDATES["pen_name"],
        required=False,
        description="pen name",
    )

    LOGGER.info(
        "Column mapping: date=%s, recipe=%s, ingredient=%s, pen=%s, dry_call=%s, dry_actual=%s",
        date_col,
        recipe_col,
        ingredient_col,
        pen_col,
        call_col,
        actual_col,
    )

    keep_cols = [date_col, recipe_col, ingredient_col, call_col, actual_col]
    if pen_col is not None:
        keep_cols.append(pen_col)
    for optional_col in ("source_file", "source_sheet"):
        if optional_col in raw_feed.columns:
            keep_cols.append(optional_col)

    df = raw_feed.loc[:, list(dict.fromkeys(keep_cols))].copy()
    rename_map = {
        date_col: "feeding_date",
        recipe_col: "recipe_display_name",
        ingredient_col: "ingredient_name_raw",
        call_col: "dry_call_weight",
        actual_col: "dry_actual_weight",
    }
    if pen_col is not None:
        rename_map[pen_col] = "pen_name"
    else:
        df["pen_name"] = pd.NA
    df = df.rename(columns=rename_map)

    df["feeding_date"] = clean_date(df["feeding_date"])
    df["recipe_display_name"] = df["recipe_display_name"].map(clean_text)
    df["ingredient_name_raw"] = df["ingredient_name_raw"].map(clean_text)
    df["ingredient_code"] = df["ingredient_name_raw"].map(
        lambda value: standardize_ingredient_code(value, ingredient_mapping)
    )
    df["pen_name"] = df["pen_name"].map(clean_text)
    df["dry_call_weight"] = clean_numeric(df["dry_call_weight"])
    df["dry_actual_weight"] = clean_numeric(df["dry_actual_weight"])

    before = len(df)
    df = df.dropna(subset=["feeding_date", "recipe_display_name", "ingredient_code"]).copy()
    df = df.dropna(subset=["dry_call_weight", "dry_actual_weight"], how="all").copy()
    LOGGER.info("Dropped %s rows missing key feed-intake fields.", before - len(df))

    if start_date is not None:
        start = pd.to_datetime(start_date).date()
        df = df[df["feeding_date"] >= start].copy()
    if end_date is not None:
        end = pd.to_datetime(end_date).date()
        df = df[df["feeding_date"] <= end].copy()

    group_cols = get_formula_group_columns(group_by_pen=group_by_pen, has_pen="pen_name" in df.columns)
    df["dry_actual_weight_sum"] = df.groupby(group_cols, dropna=False)["dry_actual_weight"].transform("sum")
    df["dry_call_weight_sum"] = df.groupby(group_cols, dropna=False)["dry_call_weight"].transform("sum")
    df["actual_pct_participation"] = safe_percent(df["dry_actual_weight"], df["dry_actual_weight_sum"])
    df["call_pct_participation"] = safe_percent(df["dry_call_weight"], df["dry_call_weight_sum"])
    # Historical notebook/table name: Pct. Participation uses actual dry-weight share.
    df["pct_participation"] = df["actual_pct_participation"]

    output_cols = [
        "feeding_date",
        "recipe_display_name",
        "ingredient_name_raw",
        "ingredient_code",
        "pen_name",
        "dry_call_weight",
        "dry_actual_weight",
        "dry_actual_weight_sum",
        "dry_call_weight_sum",
        "pct_participation",
        "call_pct_participation",
        "actual_pct_participation",
    ]
    for optional_col in ("source_file", "source_sheet"):
        if optional_col in df.columns:
            output_cols.append(optional_col)

    df = df.loc[:, output_cols].sort_values(group_cols + ["ingredient_code"]).reset_index(drop=True)
    LOGGER.info("Prepared feed-intake dataset with %s rows.", len(df))
    return df


def get_formula_group_columns(*, group_by_pen: bool = False, has_pen: bool = True) -> list[str]:
    """Return grouping columns for formula/day calculations."""
    group_cols = ["feeding_date", "recipe_display_name"]
    if group_by_pen and has_pen:
        group_cols.append("pen_name")
    return group_cols


def calculate_dry_weight_participation(filtered_feed: pd.DataFrame) -> pd.DataFrame:
    """Create ingredient-level dry-weight participation table."""
    cols = [
        "feeding_date",
        "recipe_display_name",
        "ingredient_code",
        "ingredient_name_raw",
        "pen_name",
        "dry_call_weight",
        "dry_actual_weight",
        "dry_call_weight_sum",
        "dry_actual_weight_sum",
        "call_pct_participation",
        "actual_pct_participation",
        "pct_participation",
    ]
    return filtered_feed.loc[:, [col for col in cols if col in filtered_feed.columns]].copy()


def calculate_weight_difference_ing(filtered_feed: pd.DataFrame) -> pd.DataFrame:
    """
    Create ingredient-level weight-difference table.

    The thesis table values match direct dry-weight relative error:
        (dry_actual_weight - dry_call_weight) / dry_call_weight * 100

    The participation-based error from the written formula is also retained as a
    separate column for transparency.
    """
    df = filtered_feed.copy()
    df["weight_difference_pct"] = safe_percent(
        df["dry_actual_weight"] - df["dry_call_weight"], df["dry_call_weight"]
    )
    df["participation_difference_pct"] = safe_percent(
        df["actual_pct_participation"] - df["call_pct_participation"],
        df["call_pct_participation"],
    )
    cols = [
        "feeding_date",
        "recipe_display_name",
        "ingredient_code",
        "ingredient_name_raw",
        "pen_name",
        "dry_call_weight",
        "dry_actual_weight",
        "call_pct_participation",
        "actual_pct_participation",
        "weight_difference_pct",
        "participation_difference_pct",
    ]
    return df.loc[:, [col for col in cols if col in df.columns]].copy()


def calculate_error_by_weight(
    filtered_feed: pd.DataFrame,
    *,
    group_by_pen: bool = False,
) -> pd.DataFrame:
    """Create formula-level total dry-weight error table."""
    group_cols = get_formula_group_columns(group_by_pen=group_by_pen, has_pen="pen_name" in filtered_feed.columns)
    df = (
        filtered_feed.groupby(group_cols, dropna=False)
        .agg(
            dry_actual_weight_sum=("dry_actual_weight", "sum"),
            dry_call_weight_sum=("dry_call_weight", "sum"),
        )
        .reset_index()
    )
    df["error_by_weight_pct"] = safe_percent(
        df["dry_actual_weight_sum"] - df["dry_call_weight_sum"],
        df["dry_call_weight_sum"],
    )
    return df.sort_values(group_cols).reset_index(drop=True)


def prepare_nutrient_table(
    nutrient_table_path: str | Path,
    *,
    ingredient_mapping: Optional[Mapping[str, str]] = None,
) -> pd.DataFrame:
    """Load and standardize the ingredient nutrient-composition table."""
    raw = read_table_file(Path(nutrient_table_path))

    code_col = find_column(
        raw,
        NUTRIENT_COLUMN_CANDIDATES["ingredient_code"],
        required=False,
        description="nutrient ingredient code",
    )
    ingredient_col = find_column(
        raw,
        NUTRIENT_COLUMN_CANDIDATES["ingredient_name"],
        required=False,
        description="nutrient ingredient name",
    )
    if code_col is None and ingredient_col is None:
        raise ValueError(
            "Nutrient table needs either an ingredient code column or an ingredient name column."
        )

    protein_col = find_column(
        raw,
        NUTRIENT_COLUMN_CANDIDATES["protein_pct"],
        required=False,
        description="crude protein percentage",
    )
    andfom_col = find_column(
        raw,
        NUTRIENT_COLUMN_CANDIDATES["andfom_pct"],
        required=False,
        description="aNDFom/NDF percentage",
    )
    starch_col = find_column(
        raw,
        NUTRIENT_COLUMN_CANDIDATES["starch_pct"],
        required=False,
        description="starch percentage",
    )
    date_col = find_column(
        raw,
        NUTRIENT_COLUMN_CANDIDATES["nutrient_date"],
        required=False,
        description="nutrient table date",
    )

    keep_cols = [col for col in [code_col, ingredient_col, protein_col, andfom_col, starch_col, date_col] if col]
    nutrient = raw.loc[:, list(dict.fromkeys(keep_cols))].copy()

    if code_col is not None:
        nutrient["ingredient_code"] = nutrient[code_col].map(
            lambda value: standardize_ingredient_code(value, ingredient_mapping)
        )
    else:
        nutrient["ingredient_code"] = nutrient[ingredient_col].map(
            lambda value: standardize_ingredient_code(value, ingredient_mapping)
        )

    if ingredient_col is not None:
        nutrient["ingredient_name_nutrient_table"] = nutrient[ingredient_col].map(clean_text)
    else:
        nutrient["ingredient_name_nutrient_table"] = pd.NA

    if protein_col is not None:
        nutrient["protein_pct"] = clean_numeric(nutrient[protein_col])
    else:
        nutrient["protein_pct"] = np.nan

    if andfom_col is not None:
        nutrient["andfom_pct"] = clean_numeric(nutrient[andfom_col])
    else:
        nutrient["andfom_pct"] = np.nan

    if starch_col is not None:
        nutrient["starch_pct"] = clean_numeric(nutrient[starch_col])
    else:
        nutrient["starch_pct"] = np.nan

    if date_col is not None:
        nutrient["nutrient_date"] = clean_date(nutrient[date_col])
    else:
        nutrient["nutrient_date"] = pd.NA

    nutrient = nutrient.dropna(subset=["ingredient_code"]).copy()
    nutrient = nutrient[
        [
            "ingredient_code",
            "ingredient_name_nutrient_table",
            "protein_pct",
            "andfom_pct",
            "starch_pct",
            "nutrient_date",
        ]
    ]

    # If multiple rows exist per code, use the latest dated row when date is available;
    # otherwise keep the last occurrence from the file.
    if nutrient["nutrient_date"].notna().any():
        nutrient = nutrient.sort_values(["ingredient_code", "nutrient_date"])
    nutrient = nutrient.drop_duplicates(subset=["ingredient_code"], keep="last").reset_index(drop=True)

    LOGGER.info("Prepared nutrient table with %s ingredient code(s).", len(nutrient))
    return nutrient


def calculate_nutrient_delivery_by_ingredient(
    filtered_feed: pd.DataFrame,
    nutrient_table: pd.DataFrame,
) -> pd.DataFrame:
    """Create nutrient delivery and ingredient-level nutrient error table."""
    df = filtered_feed.merge(nutrient_table, on="ingredient_code", how="left")

    missing = sorted(df.loc[df[["protein_pct", "andfom_pct", "starch_pct"]].isna().all(axis=1), "ingredient_code"].dropna().unique())
    if missing:
        LOGGER.warning(
            "Ingredient code(s) missing all nutrient values and will have NaN nutrient delivery: %s",
            ", ".join(missing),
        )

    for nutrient_name, pct_col in (
        ("protein", "protein_pct"),
        ("andfom", "andfom_pct"),
        ("starch", "starch_pct"),
    ):
        df[f"call_{nutrient_name}"] = df["dry_call_weight"] * df[pct_col] / 100.0
        df[f"actual_{nutrient_name}"] = df["dry_actual_weight"] * df[pct_col] / 100.0
        df[f"{nutrient_name}_error_pct"] = safe_percent(
            df[f"actual_{nutrient_name}"] - df[f"call_{nutrient_name}"],
            df[f"call_{nutrient_name}"],
        )

    cols = [
        "feeding_date",
        "recipe_display_name",
        "ingredient_code",
        "ingredient_name_raw",
        "ingredient_name_nutrient_table",
        "pen_name",
        "dry_call_weight",
        "dry_actual_weight",
        "protein_pct",
        "andfom_pct",
        "starch_pct",
        "call_protein",
        "actual_protein",
        "protein_error_pct",
        "call_andfom",
        "actual_andfom",
        "andfom_error_pct",
        "call_starch",
        "actual_starch",
        "starch_error_pct",
        "nutrient_date",
    ]
    return df.loc[:, [col for col in cols if col in df.columns]].copy()


def calculate_nutrient_error_by_formula(
    nutrient_delivery: pd.DataFrame,
    error_by_weight: pd.DataFrame,
    *,
    group_by_pen: bool = False,
) -> pd.DataFrame:
    """
    Create formula-level nutrient error table.

    Nutrient totals are summed by formula/day. Formula-level error is computed on
    nutrient concentration (% DM), not on the absolute nutrient totals:
        ((actual_nutrient / actual_total_dm) - (call_nutrient / call_total_dm))
        / (call_nutrient / call_total_dm) * 100
    """
    group_cols = get_formula_group_columns(group_by_pen=group_by_pen, has_pen="pen_name" in nutrient_delivery.columns)
    nutrient_cols = [
        "call_protein",
        "actual_protein",
        "call_andfom",
        "actual_andfom",
        "call_starch",
        "actual_starch",
    ]

    nutrient_sums = (
        nutrient_delivery.groupby(group_cols, dropna=False)[nutrient_cols]
        .sum(min_count=1)
        .reset_index()
    )
    formula = nutrient_sums.merge(
        error_by_weight[group_cols + ["dry_call_weight_sum", "dry_actual_weight_sum"]],
        on=group_cols,
        how="left",
    )

    for nutrient_name in ("protein", "andfom", "starch"):
        formula[f"call_{nutrient_name}_pct_dm"] = safe_percent(
            formula[f"call_{nutrient_name}"], formula["dry_call_weight_sum"]
        )
        formula[f"actual_{nutrient_name}_pct_dm"] = safe_percent(
            formula[f"actual_{nutrient_name}"], formula["dry_actual_weight_sum"]
        )
        formula[f"total_{nutrient_name}_error_pct"] = safe_percent(
            formula[f"actual_{nutrient_name}_pct_dm"] - formula[f"call_{nutrient_name}_pct_dm"],
            formula[f"call_{nutrient_name}_pct_dm"],
        )

    ordered_cols = group_cols + [
        "dry_call_weight_sum",
        "dry_actual_weight_sum",
        "call_protein",
        "actual_protein",
        "call_protein_pct_dm",
        "actual_protein_pct_dm",
        "total_protein_error_pct",
        "call_andfom",
        "actual_andfom",
        "call_andfom_pct_dm",
        "actual_andfom_pct_dm",
        "total_andfom_error_pct",
        "call_starch",
        "actual_starch",
        "call_starch_pct_dm",
        "actual_starch_pct_dm",
        "total_starch_error_pct",
    ]
    return formula.loc[:, ordered_cols].sort_values(group_cols).reset_index(drop=True)


def write_csv_outputs(tables: Mapping[str, pd.DataFrame], output_dir: str | Path) -> None:
    """Write each pipeline output table to CSV."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    for name, table in tables.items():
        csv_path = output_path / f"{name}.csv"
        table.to_csv(csv_path, index=False)
        LOGGER.info("Wrote %s rows to %s", len(table), csv_path)


def build_postgres_engine_from_env():
    """Build a SQLAlchemy engine from POSTGRES_* environment variables."""
    from sqlalchemy import create_engine

    required = [
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    ]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise EnvironmentError(
            "Missing PostgreSQL environment variable(s): " + ", ".join(missing)
        )

    user = quote_plus(os.environ["POSTGRES_USER"])
    password = quote_plus(os.environ["POSTGRES_PASSWORD"])
    host = os.environ["POSTGRES_HOST"]
    port = os.environ["POSTGRES_PORT"]
    database = os.environ["POSTGRES_DB"]
    url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"
    return create_engine(url, pool_pre_ping=True)


def write_tables_to_postgres(
    tables: Mapping[str, pd.DataFrame],
    *,
    table_names: Mapping[str, str] = POSTGRES_TABLE_NAMES,
    if_exists: str = "replace",
    schema: Optional[str] = None,
    chunksize: int = 10000,
) -> None:
    """Write output tables to PostgreSQL using pandas.to_sql()."""
    from sqlalchemy import text

    if if_exists not in {"fail", "replace", "append"}:
        raise ValueError("if_exists must be one of: fail, replace, append")

    engine = build_postgres_engine_from_env()
    schema_name = schema if schema is not None else os.getenv("POSTGRES_SCHEMA")

    with engine.begin() as connection:
        if schema_name:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))

    for key, table in tables.items():
        sql_table_name = table_names.get(key, key)
        LOGGER.info(
            "Writing %s rows to PostgreSQL table %s%s",
            len(table),
            f"{schema_name}." if schema_name else "",
            sql_table_name,
        )
        table.to_sql(
            sql_table_name,
            con=engine,
            schema=schema_name,
            if_exists=if_exists,
            index=False,
            method="multi",
            chunksize=chunksize,
        )


def run_pipeline(
    feed_intake_path: str | Path = DEFAULT_FEED_INTAKE_PATH,
    nutrient_table_path: str | Path = DEFAULT_NUTRIENT_TABLE_PATH,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    *,
    ingredient_map_path: Optional[str | Path] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    group_by_pen: bool = False,
    write_csv: bool = True,
    write_postgres: bool = False,
    postgres_if_exists: str = "replace",
    postgres_schema: Optional[str] = None,
) -> dict[str, pd.DataFrame]:
    """
    Run the complete Chapter 4 Feed Traits pipeline.

    Returns a dictionary of pandas DataFrames keyed by output name.
    """
    ingredient_mapping = load_mapping_file(ingredient_map_path)
    raw_feed = load_feed_intake_data(feed_intake_path)
    filtered_feed = prepare_feed_intake_data(
        raw_feed,
        ingredient_mapping=ingredient_mapping,
        start_date=start_date,
        end_date=end_date,
        group_by_pen=group_by_pen,
    )
    nutrient_table = prepare_nutrient_table(
        nutrient_table_path,
        ingredient_mapping=ingredient_mapping,
    )

    dry_weight_participation = calculate_dry_weight_participation(filtered_feed)
    weight_difference_ing = calculate_weight_difference_ing(filtered_feed)
    error_by_weight = calculate_error_by_weight(filtered_feed, group_by_pen=group_by_pen)
    nutrient_delivery_by_ingredient = calculate_nutrient_delivery_by_ingredient(
        filtered_feed,
        nutrient_table,
    )
    nutrient_error_by_formula = calculate_nutrient_error_by_formula(
        nutrient_delivery_by_ingredient,
        error_by_weight,
        group_by_pen=group_by_pen,
    )

    tables = {
        "filtered_feed_intake": filtered_feed,
        "dry_weight_participation": dry_weight_participation,
        "weight_difference_ing": weight_difference_ing,
        "error_by_weight": error_by_weight,
        "nutrient_delivery_by_ingredient": nutrient_delivery_by_ingredient,
        "nutrient_error_by_formula": nutrient_error_by_formula,
    }

    if write_csv:
        write_csv_outputs(tables, output_dir)

    if write_postgres:
        write_tables_to_postgres(
            tables,
            if_exists=postgres_if_exists,
            schema=postgres_schema,
        )

    return tables


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Chapter 4 Feed Traits container pipeline."
    )
    parser.add_argument(
        "--feed-intake-dir",
        "--feed-intake-path",
        dest="feed_intake_path",
        type=Path,
        default=DEFAULT_FEED_INTAKE_PATH,
        help="Directory or file containing TMR Tracker feed-intake data. Default: /data/feed-intake",
    )
    parser.add_argument(
        "--nutrient-table",
        type=Path,
        default=DEFAULT_NUTRIENT_TABLE_PATH,
        help="CSV/Excel/Parquet nutrient-composition table. Default: /data/nutrient-table.csv",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for CSV backup outputs. Default: /outputs/chapter4",
    )
    parser.add_argument(
        "--ingredient-map",
        type=Path,
        default=None,
        help="Optional CSV/Excel/Parquet mapping file from raw ingredient names to ingredient codes.",
    )
    parser.add_argument("--start-date", default=None, help="Optional inclusive start date, YYYY-MM-DD.")
    parser.add_argument("--end-date", default=None, help="Optional inclusive end date, YYYY-MM-DD.")
    parser.add_argument(
        "--group-by-pen",
        action="store_true",
        help="Group formula-level outputs by feeding date, recipe, and pen instead of feeding date and recipe only.",
    )
    parser.add_argument(
        "--no-csv",
        action="store_true",
        help="Do not write CSV backup outputs.",
    )
    parser.add_argument(
        "--write-postgres",
        action="store_true",
        help="Write all output tables to PostgreSQL using POSTGRES_* environment variables.",
    )
    parser.add_argument(
        "--postgres-if-exists",
        choices=["fail", "replace", "append"],
        default="replace",
        help="Behavior when PostgreSQL tables already exist. Default: replace.",
    )
    parser.add_argument(
        "--postgres-schema",
        default=None,
        help="Optional PostgreSQL schema override. If omitted, POSTGRES_SCHEMA is used.",
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

    run_pipeline(
        feed_intake_path=args.feed_intake_path,
        nutrient_table_path=args.nutrient_table,
        output_dir=args.output_dir,
        ingredient_map_path=args.ingredient_map,
        start_date=args.start_date,
        end_date=args.end_date,
        group_by_pen=args.group_by_pen,
        write_csv=not args.no_csv,
        write_postgres=args.write_postgres,
        postgres_if_exists=args.postgres_if_exists,
        postgres_schema=args.postgres_schema,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
