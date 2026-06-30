from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from .db import (
    DEFAULT_KEYRING_SERVICE,
    DEFAULT_PROPERTY_TABLE,
    DEFAULT_PROPID_COLUMN,
    PostgresConfig,
    PostgresPropertyIdConnector,
    upload_normalized_csv,
)
from .env import load_env_files
from .logging_config import setup_logging
from .pipeline import download_and_upload_to_db, write_normalized_csv
from .weekly_dat import discover_weekly_zip_dates
from .weekly_pipeline import run_weekly_dat_upload


def main() -> None:
    load_env_files()

    parser = argparse.ArgumentParser(description="Download and normalize NSW SIX property sales CSV files.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity.")
    parser.add_argument("--log-file", help="Optional log file path.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample_parser = subparsers.add_parser("sample", help="Download one or more explicit propids.")
    add_logging_args(sample_parser)
    sample_parser.add_argument("--propid", action="append", type=int, required=True)
    add_common_args(sample_parser)

    db_parser = subparsers.add_parser("db", help="Stream propids from PostgreSQL.")
    add_logging_args(db_parser)
    add_db_args(db_parser, dsn_required=False)
    add_common_args(db_parser)

    test_parser = subparsers.add_parser("db-test", help="Test PostgreSQL connection and preview propids.")
    add_logging_args(test_parser)
    add_db_args(test_parser, dsn_required=False)
    test_parser.add_argument("--preview-limit", type=int, default=10, help="Number of propids to preview.")

    upload_parser = subparsers.add_parser("upload", help="Upload normalized CSV rows into PostgreSQL.")
    add_logging_args(upload_parser)
    add_db_connection_args(upload_parser, dsn_required=False)
    upload_parser.add_argument("--input", required=True, help="Normalized CSV path to upload.")
    upload_parser.add_argument("--target-table", default=DEFAULT_PROPERTY_TABLE, help="Target PostgreSQL table.")

    db_upload_parser = subparsers.add_parser("db-upload", help="Download, transform, and upload directly without a local normalized CSV.")
    add_logging_args(db_upload_parser)
    add_db_args(db_upload_parser, dsn_required=False)
    db_upload_parser.add_argument("--target-table", default=DEFAULT_PROPERTY_TABLE, help="Target PostgreSQL table.")
    db_upload_parser.add_argument("--workers", type=int, default=8, help="Parallel download workers. Start with 8-16 for large runs.")
    db_upload_parser.add_argument("--upload-workers", type=int, default=1, help="Parallel database upload workers.")
    db_upload_parser.add_argument("--upload-batch-rows", type=int, default=10000, help="Rows per database upload batch.")
    db_upload_parser.add_argument("--progress-interval", type=int, default=1000, help="Log summary progress every N downloaded CSV files.")
    db_upload_parser.add_argument("--skipped-output", help="Optional CSV path for propids where no source CSV exists.")
    db_upload_parser.add_argument("--failed-output", help="Optional CSV path for non-404 failures. If omitted, failures stop the run.")

    weekly_list_parser = subparsers.add_parser("weekly-list", help="List weekly DAT ZIP dates available on the NSW valuation site.")
    add_logging_args(weekly_list_parser)

    weekly_upload_parser = subparsers.add_parser("weekly-upload", help="Download weekly DAT ZIP data, transform it, and upload it to PostgreSQL.")
    add_logging_args(weekly_upload_parser)
    add_db_connection_args(weekly_upload_parser, dsn_required=False)
    weekly_upload_parser.add_argument("--target-table", default=DEFAULT_PROPERTY_TABLE, help="Target PostgreSQL table.")
    weekly_upload_parser.add_argument("--week", action="append", type=parse_iso_date, help="Week date to download, for example 2026-06-29. Can be repeated.")
    weekly_upload_parser.add_argument("--latest", action="store_true", help="Download only the latest week listed on the website.")
    weekly_upload_parser.add_argument("--work-dir", default="data\\valuation_weekly", help="D-drive work directory for ZIP cache and skipped-row reports.")
    weekly_upload_parser.add_argument("--no-keep-zip", action="store_true", help="Do not keep downloaded weekly ZIP files.")

    args = parser.parse_args()
    setup_logging(args.log_level, Path(args.log_file) if args.log_file else None)

    if args.command == "sample":
        propids = args.propid
    elif args.command == "db-test":
        connector = build_connector(args)
        version = connector.test_connection()
        preview = connector.preview_propids(args.preview_limit)
        print(f"Connected to PostgreSQL: {version}")
        print(f"Preview propid(s): {', '.join(str(propid) for propid in preview)}")
        return
    elif args.command == "upload":
        inserted = upload_normalized_csv(build_db_config(args), Path(args.input), target_table=args.target_table)
        print(f"Uploaded {inserted} row(s) from {args.input} into {args.target_table}.")
        return
    elif args.command == "weekly-list":
        dates = discover_weekly_zip_dates()
        print("\n".join(date.isoformat() for date in dates))
        return
    elif args.command == "db-upload":
        result = download_and_upload_to_db(
            build_connector(args).iter_propids(),
            build_db_config(args),
            target_table=args.target_table,
            skipped_path=Path(args.skipped_output) if args.skipped_output else None,
            failed_path=Path(args.failed_output) if args.failed_output else None,
            workers=args.workers,
            upload_workers=args.upload_workers,
            upload_batch_rows=args.upload_batch_rows,
            progress_interval=args.progress_interval,
        )
        print(
            f"Uploaded {result.uploaded_rows} row(s) into {args.target_table}; "
            f"staged {result.staged_rows} row(s) in {result.upload_batches} upload batch(es), "
            f"downloaded {result.downloaded_files} CSV file(s), "
            f"skipped {result.skipped_missing} missing CSV file(s), "
            f"failed {result.failed} propid(s)."
        )
        return
    elif args.command == "weekly-upload":
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
        return
    else:
        propids = build_connector(args).iter_propids()

    count = write_normalized_csv(
        propids,
        Path(args.output),
        raw_dir=Path(args.raw_dir) if args.raw_dir else None,
        limit=args.limit,
        skipped_path=Path(args.skipped_output) if args.skipped_output else None,
        failed_path=Path(args.failed_output) if args.failed_output else None,
        workers=args.workers,
        progress_interval=args.progress_interval,
    )
    print(
        f"Wrote {count.written_rows} normalized row(s) to {args.output}; "
        f"downloaded {count.downloaded_files} CSV file(s), "
        f"skipped {count.skipped_missing} missing CSV file(s), "
        f"failed {count.failed} propid(s)."
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", required=True, help="Output normalized CSV path.")
    parser.add_argument("--raw-dir", help="Optional directory to store raw downloaded CSV files.")
    parser.add_argument("--limit", type=int, help="Maximum normalized rows to write.")
    parser.add_argument("--skipped-output", help="Optional CSV path for propids where no source CSV exists.")
    parser.add_argument("--failed-output", help="Optional CSV path for non-404 failures. If omitted, failures stop the run.")
    parser.add_argument("--workers", type=int, default=8, help="Parallel download workers. Start with 8-16 for large runs.")
    parser.add_argument("--progress-interval", type=int, default=1000, help="Log summary progress every N downloaded CSV files.")


def add_logging_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity.")
    parser.add_argument("--log-file", help="Optional log file path.")


def add_db_args(parser: argparse.ArgumentParser, *, dsn_required: bool) -> None:
    add_db_connection_args(parser, dsn_required=dsn_required)
    parser.add_argument("--propid-table", default=DEFAULT_PROPERTY_TABLE, help="Table containing propids.")
    parser.add_argument("--propid-column", default=DEFAULT_PROPID_COLUMN, help="Propid column name.")
    parser.add_argument("--propid-query", help="Custom query returning propids as the first column.")
    parser.add_argument("--batch-size", type=int, default=10_000)
    parser.add_argument("--no-distinct", action="store_true", help="Do not add DISTINCT when reading propids from --propid-table.")


def add_db_connection_args(parser: argparse.ArgumentParser, *, dsn_required: bool) -> None:
    parser.add_argument("--dsn", required=dsn_required, help="PostgreSQL DSN. Defaults to PGDSN env var if omitted.")
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


def build_connector(args: argparse.Namespace) -> PostgresPropertyIdConnector:
    return PostgresPropertyIdConnector(
        build_db_config(args),
        propid_table=args.propid_table,
        propid_column=args.propid_column,
        propid_query=args.propid_query,
        batch_size=args.batch_size,
        distinct=not args.no_distinct,
    )


def parse_iso_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use YYYY-MM-DD, for example 2026-06-29.") from exc


if __name__ == "__main__":
    main()
