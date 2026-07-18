from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
DB_CONNECTOR_SRC = Path(r"D:\projects\db_connector\src")
for path in (SRC_DIR, DB_CONNECTOR_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from db_connector import DatabaseConfig, PostgresConnector, quote_identifier, quote_qualified_identifier
from six_nsw_property_download.db import UPLOAD_COLUMNS, coerce_upload_value
from six_nsw_property_download.timezone import HONG_KONG_TZ
from six_nsw_property_download.transform import OUTPUT_COLUMNS
from six_nsw_property_download.weekly_dat import WeeklyZip, dat_duplicate_key, dedupe_rows, parse_weekly_zip, validate_output_schema
from six_nsw_property_download.weekly_pipeline import split_uploadable_rows


DEFAULT_TARGET_TABLE = "propdb_staging.nsw_property_sales_all_history"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export weekly DAT rows that are not already in PostgreSQL, using D:\\projects\\db_connector."
    )
    parser.add_argument("--week", action="append", type=parse_iso_date, required=True)
    parser.add_argument("--work-dir", default="data\\valuation_weekly")
    parser.add_argument("--output", required=True)
    parser.add_argument("--target-table", default=DEFAULT_TARGET_TABLE)
    parser.add_argument("--db-host", default="100.124.134.29")
    parser.add_argument("--db-port", type=int, default=5432)
    parser.add_argument("--db-name", default="postgres")
    parser.add_argument("--db-user", default="banner17")
    parser.add_argument("--db-password-keyring-service", default="banner17")
    parser.add_argument("--db-password-keyring-username", default="banner17")
    parser.add_argument("--db-sslmode")
    args = parser.parse_args()

    config = DatabaseConfig(
        host=args.db_host,
        port=args.db_port,
        database=args.db_name,
        user=args.db_user,
        sslmode=args.db_sslmode,
        password_keyring_service=args.db_password_keyring_service,
        password_keyring_username=args.db_password_keyring_username,
    )
    connector = PostgresConnector(config)
    assert_target_table(connector, args.target_table)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    zip_dir = Path(args.work_dir) / "zip"

    all_new_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []

    for week in args.week:
        zip_path = zip_dir / f"{week.strftime('%Y%m%d')}.zip"
        content = zip_path.read_bytes()
        weekly_zip = WeeklyZip(
            week=week,
            url=f"https://www.valuergeneral.nsw.gov.au/__psi/weekly/{week.strftime('%Y%m%d')}.zip",
            content=content,
        )
        parsed = parse_weekly_zip(weekly_zip)
        validate_output_schema(parsed.rows)
        unique_rows, duplicate_rows = dedupe_rows(parsed.rows)
        uploadable_rows, invalid_rows = split_uploadable_rows(unique_rows)
        existing_rows = find_existing_dat_rows(connector, uploadable_rows, target_table=args.target_table)
        existing_keys = {dat_duplicate_key(row) for row in existing_rows}
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
                "existing_rows": len(existing_rows),
                "new_rows": len(new_rows),
            }
        )
        print(
            f"{week.isoformat()}: parsed={len(parsed.rows)} uploadable={len(uploadable_rows)} "
            f"existing={len(existing_rows)} new={len(new_rows)}"
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


def assert_target_table(connector: PostgresConnector, target_table: str) -> None:
    schema, table = split_qualified_table(target_table)
    exists = connector.fetch_one(
        """
        select 1
        from information_schema.tables
        where table_schema = %s and table_name = %s
        """,
        (schema, table),
    )
    if not exists:
        raise RuntimeError(f"Target table was not found: {target_table}")


def find_existing_dat_rows(
    connector: PostgresConnector,
    rows: list[dict[str, object]],
    *,
    target_table: str,
    columns: list[str] = UPLOAD_COLUMNS,
) -> list[dict[str, Any]]:
    if not rows:
        return []

    table_sql = quote_qualified_identifier(target_table)
    temp_table_sql = quote_identifier("six_weekly_dat_check_temp")
    columns_sql = ", ".join(quote_identifier(column) for column in columns)
    copy_sql = f"COPY {temp_table_sql} ({columns_sql}) FROM STDIN"
    duplicate_predicate = (
        "t.url_property_id IS NOT DISTINCT FROM s.url_property_id "
        "AND t.sale_date IS NOT DISTINCT FROM s.sale_date "
        "AND t.sale_price IS NOT DISTINCT FROM s.sale_price "
        "AND COALESCE(t.dealing_number, '') = COALESCE(s.dealing_number, '')"
    )
    existing_sql = (
        f"SELECT {columns_sql} "
        f"FROM {temp_table_sql} s "
        f"WHERE EXISTS (SELECT 1 FROM {table_sql} t WHERE {duplicate_predicate})"
    )

    imported_at = datetime.now(HONG_KONG_TZ)
    with connector.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE TEMP TABLE {temp_table_sql} (LIKE {table_sql} INCLUDING DEFAULTS) ON COMMIT DROP")
            with cur.copy(copy_sql) as copy:
                for row in rows:
                    values = [coerce_upload_value(column, row.get(column), imported_at) for column in columns]
                    copy.write_row(values)
            cur.execute(existing_sql)
            existing_rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
        conn.commit()
    return existing_rows


def split_qualified_table(value: str) -> tuple[str, str]:
    parts = value.split(".")
    if len(parts) != 2:
        raise ValueError("Use a schema-qualified table name like schema.table.")
    return parts[0], parts[1]


def parse_iso_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use YYYY-MM-DD, for example 2026-06-29.") from exc


if __name__ == "__main__":
    main()
