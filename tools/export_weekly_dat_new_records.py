from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from six_nsw_property_download.db import DEFAULT_KEYRING_SERVICE, DEFAULT_PROPERTY_TABLE, PostgresConfig, check_dat_rows_against_db
from six_nsw_property_download.env import load_env_files
from six_nsw_property_download.transform import OUTPUT_COLUMNS
from six_nsw_property_download.weekly_dat import dat_duplicate_key, dedupe_rows, download_weekly_zip, parse_weekly_zip, validate_output_schema
from six_nsw_property_download.weekly_pipeline import split_uploadable_rows


def main() -> None:
    load_env_files()
    parser = argparse.ArgumentParser(description="Export weekly DAT rows that do not already exist in PostgreSQL.")
    parser.add_argument("--week", action="append", type=parse_iso_date, required=True)
    parser.add_argument("--work-dir", default="data\\valuation_weekly")
    parser.add_argument("--target-table", default=DEFAULT_PROPERTY_TABLE)
    parser.add_argument("--output", required=True)
    add_db_connection_args(parser)
    args = parser.parse_args()

    config = build_db_config(args)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    all_new_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    zip_dir = Path(args.work_dir) / "zip"

    for week in args.week:
        weekly_zip = download_weekly_zip(week, output_dir=zip_dir)
        parsed = parse_weekly_zip(weekly_zip)
        validate_output_schema(parsed.rows)
        unique_rows, duplicate_rows = dedupe_rows(parsed.rows)
        uploadable_rows, invalid_rows = split_uploadable_rows(unique_rows)
        check_result = check_dat_rows_against_db(config, uploadable_rows, target_table=args.target_table)
        existing_keys = {dat_duplicate_key(row) for row in check_result.existing_rows}
        new_rows = [row for row in uploadable_rows if dat_duplicate_key(row) not in existing_keys]
        for row in new_rows:
            row["dry_run_week"] = week.isoformat()
        all_new_rows.extend(new_rows)
        summary_rows.append(
            {
                "week": week.isoformat(),
                "parsed_rows": len(parsed.rows),
                "source_duplicate_rows": len(duplicate_rows),
                "invalid_rows": len(invalid_rows),
                "uploadable_rows": len(uploadable_rows),
                "existing_rows": len(check_result.existing_rows),
                "new_rows": len(new_rows),
            }
        )

    columns = ["dry_run_week"] + [column for column in OUTPUT_COLUMNS if column != "id"]
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_new_rows)

    summary_path = output_path.with_suffix(".summary.csv")
    with summary_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(summary_rows[0].keys()) if summary_rows else [])
        writer.writeheader()
        writer.writerows(summary_rows)

    print(f"new_records_csv={output_path}")
    print(f"summary_csv={summary_path}")
    print(f"new_records={len(all_new_rows)}")


def add_db_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dsn", help="PostgreSQL DSN. Defaults to PGDSN env var if omitted.")
    parser.add_argument("--db-host", help="PostgreSQL host. Defaults to PGHOST env var.")
    parser.add_argument("--db-port", type=int, help="PostgreSQL port. Defaults to PGPORT env var.")
    parser.add_argument("--db-name", help="PostgreSQL database name. Defaults to PGDATABASE or PGDB env var.")
    parser.add_argument("--db-user", help="PostgreSQL user. Defaults to PGUSER env var.")
    parser.add_argument("--db-password", help="PostgreSQL password. Defaults to PGPASSWORD env var.")
    parser.add_argument(
        "--db-password-keyring-service",
        default=None,
        help=f"Keyring service name for PostgreSQL password. Defaults to PGPASSWORD_KEYRING_SERVICE or {DEFAULT_KEYRING_SERVICE}.",
    )
    parser.add_argument("--db-password-keyring-username", help="Keyring username for PostgreSQL password. Defaults to the DB user.")
    parser.add_argument("--db-sslmode", help="PostgreSQL sslmode. Defaults to PGSSLMODE env var.")


def build_db_config(args: argparse.Namespace) -> PostgresConfig:
    env_config = PostgresConfig.from_env()
    return PostgresConfig(
        host=args.db_host or env_config.host,
        port=args.db_port or env_config.port,
        database=args.db_name or env_config.database,
        user=args.db_user or env_config.user,
        password=args.db_password or env_config.password,
        sslmode=args.db_sslmode or env_config.sslmode,
        dsn=args.dsn or env_config.dsn,
        password_keyring_service=args.db_password_keyring_service or env_config.password_keyring_service,
        password_keyring_username=args.db_password_keyring_username or env_config.password_keyring_username,
    )


def parse_iso_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use YYYY-MM-DD, for example 2026-06-29.") from exc


if __name__ == "__main__":
    main()
