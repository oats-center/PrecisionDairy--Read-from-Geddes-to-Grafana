#!/usr/bin/env python3
"""
Run the full dairy processing workflow inside the container.

Order:
  1. Chapter 3 herd pipeline -> CSV outputs in /outputs/chapter3
  2. Chapter 4 feed pipeline -> CSV outputs in /outputs/chapter4
  3. Load all generated CSV outputs into PostgreSQL

This file is intentionally thin. The chapter scripts do the calculations; this
script only coordinates them so IT can run one container command.

Default container paths:
  Chapter 3 input:       /data/afi
  Chapter 3 weights:     /data/allWeights.csv
  Chapter 3 outputs:     /outputs/chapter3
  Chapter 4 input:       /data/feed-intake
  Chapter 4 nutrients:   /data/nutrient-table.csv
  Chapter 4 outputs:     /outputs/chapter4

PostgreSQL is configured through environment variables used by
load_chapter_outputs_to_postgres.py:
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
import subprocess
import sys
from pathlib import Path
from typing import Optional

LOGGER = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parent


def env_path(name: str, default: str) -> Path:
    """Read a path from an environment variable, with a container-friendly default."""
    return Path(os.getenv(name, default))


def run_command(cmd: list[str], *, dry_run: bool = False) -> None:
    """Run one command and stop the workflow if it fails."""
    printable = " ".join(str(part) for part in cmd)
    LOGGER.info("Running: %s", printable)
    if dry_run:
        return
    subprocess.run(cmd, check=True)


def add_optional_arg(cmd: list[str], flag: str, value: Optional[object]) -> None:
    """Append an optional CLI flag only when a value was provided."""
    if value is not None and value != "":
        cmd.extend([flag, str(value)])


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Chapter 3, Chapter 4, then load generated CSVs to PostgreSQL."
    )

    # Script locations. These default to files copied into the same folder as run_all.py.
    parser.add_argument("--chapter3-script", type=Path, default=APP_DIR / "chapter3_herd_pipeline.py")
    parser.add_argument("--chapter4-script", type=Path, default=APP_DIR / "chapter4_feed_pipeline.py")
    parser.add_argument("--postgres-loader-script", type=Path, default=APP_DIR / "load_chapter_outputs_to_postgres.py")

    # Chapter 3 paths.
    parser.add_argument("--afi-dir", type=Path, default=env_path("CH3_AFI_DIR", "/data/afi"))
    parser.add_argument("--weights-csv", type=Path, default=env_path("CH3_WEIGHTS_CSV", "/data/allWeights.csv"))
    parser.add_argument("--chapter3-output-dir", type=Path, default=env_path("CH3_OUTPUT_DIR", "/outputs/chapter3"))
    parser.add_argument("--chapter3-start-date", default=os.getenv("CH3_START_DATE"))
    parser.add_argument("--confidence-level", type=float, default=float(os.getenv("CH3_CONFIDENCE_LEVEL", "0.80")))
    parser.add_argument("--weight-year", type=int, default=int(os.getenv("CH3_WEIGHT_YEAR")) if os.getenv("CH3_WEIGHT_YEAR") else None)
    parser.add_argument("--recursive", action="store_true", help="Pass --recursive to Chapter 3 when Afi files are nested.")
    parser.add_argument("--write-parquet", action="store_true", help="Pass --write-parquet to Chapter 3, if supported.")
    parser.add_argument(
        "--no-weights-csv",
        action="store_true",
        help="Do not pass --weights-csv to Chapter 3. Use this if body-weight data is not mounted yet.",
    )

    # Chapter 4 paths.
    parser.add_argument("--feed-intake-dir", type=Path, default=env_path("CH4_FEED_INTAKE_DIR", "/data/feed-intake"))
    parser.add_argument("--nutrient-table", type=Path, default=env_path("CH4_NUTRIENT_TABLE", "/data/nutrient-table.csv"))
    parser.add_argument("--chapter4-output-dir", type=Path, default=env_path("CH4_OUTPUT_DIR", "/outputs/chapter4"))
    parser.add_argument("--ingredient-map", type=Path, default=env_path("CH4_INGREDIENT_MAP", "") if os.getenv("CH4_INGREDIENT_MAP") else None)
    parser.add_argument("--chapter4-start-date", default=os.getenv("CH4_START_DATE"))
    parser.add_argument("--chapter4-end-date", default=os.getenv("CH4_END_DATE"))
    parser.add_argument("--group-by-pen", action="store_true", help="Pass --group-by-pen to Chapter 4.")

    # PostgreSQL loading.
    parser.add_argument("--postgres-schema", default=os.getenv("POSTGRES_SCHEMA"))
    parser.add_argument("--postgres-if-exists", choices=["replace", "append", "fail"], default=os.getenv("POSTGRES_IF_EXISTS", "replace"))
    parser.add_argument("--postgres-chunksize", type=int, default=int(os.getenv("POSTGRES_CHUNKSIZE", "10000")))

    # Workflow switches.
    parser.add_argument("--skip-chapter3", action="store_true")
    parser.add_argument("--skip-chapter4", action="store_true")
    parser.add_argument("--skip-postgres", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them.")
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    python = sys.executable

    if not args.skip_chapter3:
        cmd = [
            python,
            str(args.chapter3_script),
            "--afi-dir",
            str(args.afi_dir),
            "--output-dir",
            str(args.chapter3_output_dir),
            "--confidence-level",
            str(args.confidence_level),
        ]
        if not args.no_weights_csv:
            cmd.extend(["--weights-csv", str(args.weights_csv)])
        add_optional_arg(cmd, "--start-date", args.chapter3_start_date)
        add_optional_arg(cmd, "--weight-year", args.weight_year)
        if args.recursive:
            cmd.append("--recursive")
        if args.write_parquet:
            cmd.append("--write-parquet")
        run_command(cmd, dry_run=args.dry_run)

    if not args.skip_chapter4:
        cmd = [
            python,
            str(args.chapter4_script),
            "--feed-intake-dir",
            str(args.feed_intake_dir),
            "--nutrient-table",
            str(args.nutrient_table),
            "--output-dir",
            str(args.chapter4_output_dir),
        ]
        add_optional_arg(cmd, "--ingredient-map", args.ingredient_map)
        add_optional_arg(cmd, "--start-date", args.chapter4_start_date)
        add_optional_arg(cmd, "--end-date", args.chapter4_end_date)
        if args.group_by_pen:
            cmd.append("--group-by-pen")
        run_command(cmd, dry_run=args.dry_run)

    if not args.skip_postgres:
        cmd = [
            python,
            str(args.postgres_loader_script),
            "--chapter3-dir",
            str(args.chapter3_output_dir),
            "--chapter4-dir",
            str(args.chapter4_output_dir),
            "--if-exists",
            args.postgres_if_exists,
            "--chunksize",
            str(args.postgres_chunksize),
        ]
        add_optional_arg(cmd, "--schema", args.postgres_schema)
        if args.skip_chapter3:
            cmd.append("--skip-chapter3")
        if args.skip_chapter4:
            cmd.append("--skip-chapter4")
        run_command(cmd, dry_run=args.dry_run)

    LOGGER.info("Workflow complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
