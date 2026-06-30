from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from six_nsw_property_download.db import DEFAULT_KEYRING_SERVICE, DEFAULT_PROPERTY_TABLE, PostgresConfig
from six_nsw_property_download.env import load_env_files
from six_nsw_property_download.logging_config import setup_logging
from six_nsw_property_download.weekly_dat import discover_weekly_zip_dates
from six_nsw_property_download.weekly_pipeline import run_weekly_dat_upload


def main() -> None:
    load_env_files()

    parser = argparse.ArgumentParser(
        description="Dedicated NSW Valuer General weekly DAT downloader/uploader. This does not run the SIX/maps CSV pipeline."
    )
    parser.add_argument("--list", action="store_true", help="List available weekly DAT dates and exit.")
    parser.add_argument("--week", action="append", type=parse_iso_date, help="Week date to download, for example 2026-06-29. Can be repeated.")
    parser.add_argument("--latest", action="store_true", help="Download only the latest week listed on the website.")
    parser.add_argument("--work-dir", default="data\\valuation_weekly", help="D-drive work directory for ZIP cache and skipped-row reports.")
    parser.add_argument("--no-keep-zip", action="store_true", help="Do not keep downloaded weekly ZIP files.")
    parser.add_argument("--target-table", default=DEFAULT_PROPERTY_TABLE, help="Target PostgreSQL table.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity.")
    parser.add_argument("--log-file", default="logs\\weekly_dat_upload.log", help="Optional log file path.")
    add_db_connection_args(parser)

    args = parser.parse_args()
    setup_logging(args.log_level, Path(args.log_file) if args.log_file else None)

    if args.list:
        dates = discover_weekly_zip_dates()
        print("\n".join(date.isoformat() for date in dates))
        return

    result = run_weekly_dat_upload(
        build_db_config(args),
        target_table=args.target_table,
        weeks=args.week,
        latest=args.latest,
        work_dir=Path(args.work_dir),
        keep_zip=not args.no_keep_zip,
    )
    print(
        f"Weekly DAT upload complete for {', '.join(week.isoformat() for week in result.weeks)}; "
        f"uploaded {result.inserted_rows} row(s), staged {result.staged_rows} row(s), "
        f"read {result.downloaded_files} DAT file(s), skipped {result.skipped_rows} duplicate row(s)."
    )
    if result.skipped_report:
        print(f"Skipped-row report: {result.skipped_report}")


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
    parser.add_argument(
        "--db-password-keyring-username",
        help="Keyring username for PostgreSQL password. Defaults to PGPASSWORD_KEYRING_USERNAME or the DB user.",
    )
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
