from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
DB_CONNECTOR_SRC = Path(r"D:\projects\db_connector\src")
for path in (SRC_DIR, DB_CONNECTOR_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from db_connector import DatabaseConfig, PostgresConnector, quote_identifier, quote_qualified_identifier
from six_nsw_property_download.db import UPLOAD_COLUMNS, coerce_upload_value
from six_nsw_property_download.timezone import HONG_KONG_TZ


DEFAULT_TARGET_TABLE = "propdb_staging.nsw_property_sales_all_history"


@dataclass(frozen=True)
class UploadResult:
    csv_rows: int
    preexisting_rows: int
    inserted_rows: int
    conflict_or_unreported_rows: int
    before_count: int
    after_count: int


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload DB-checked weekly DAT rows using D:\\projects\\db_connector.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--target-table", default=DEFAULT_TARGET_TABLE)
    parser.add_argument("--db-host", default="100.124.134.29")
    parser.add_argument("--db-port", type=int, default=5432)
    parser.add_argument("--db-name", default="banner17_master")
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
    result = upload_csv(connector, Path(args.input), target_table=args.target_table)
    print(f"csv_rows={result.csv_rows}")
    print(f"preexisting_rows={result.preexisting_rows}")
    print(f"inserted_rows={result.inserted_rows}")
    print(f"conflict_or_unreported_rows={result.conflict_or_unreported_rows}")
    print(f"before_count={result.before_count}")
    print(f"after_count={result.after_count}")
    print(f"count_delta={result.after_count - result.before_count}")


def upload_csv(connector: PostgresConnector, csv_path: Path, *, target_table: str) -> UploadResult:
    table_sql = quote_qualified_identifier(target_table)
    temp_table_sql = quote_identifier("six_weekly_dat_upload_temp")
    columns = UPLOAD_COLUMNS
    columns_sql = ", ".join(quote_identifier(column) for column in columns)
    prefixed_columns_sql = ", ".join(f"s.{quote_identifier(column)}" for column in columns)
    copy_sql = f"COPY {temp_table_sql} ({columns_sql}) FROM STDIN"
    duplicate_predicate = (
        "t.url_property_id IS NOT DISTINCT FROM s.url_property_id "
        "AND t.sale_date IS NOT DISTINCT FROM s.sale_date "
        "AND t.sale_price IS NOT DISTINCT FROM s.sale_price "
        "AND COALESCE(t.dealing_number, '') = COALESCE(s.dealing_number, '')"
    )
    skipped_sql = f"SELECT count(*) FROM {temp_table_sql} s WHERE EXISTS (SELECT 1 FROM {table_sql} t WHERE {duplicate_predicate})"
    insert_sql = (
        "WITH candidates AS ("
        f"SELECT {prefixed_columns_sql} "
        f"FROM {temp_table_sql} s "
        f"WHERE NOT EXISTS (SELECT 1 FROM {table_sql} t WHERE {duplicate_predicate})"
        "), inserted AS ("
        f"INSERT INTO {table_sql} ({columns_sql}) "
        f"SELECT {columns_sql} FROM candidates "
        "ON CONFLICT DO NOTHING "
        "RETURNING 1"
        ") SELECT count(*) FROM inserted"
    )

    imported_at = datetime.now(HONG_KONG_TZ)
    copied = 0
    with connector.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table_sql}")
            before_count = int(cur.fetchone()[0])
            cur.execute(f"CREATE TEMP TABLE {temp_table_sql} (LIKE {table_sql} INCLUDING DEFAULTS) ON COMMIT DROP")
            with cur.copy(copy_sql) as copy:
                for row in iter_csv_rows(csv_path):
                    values = [coerce_upload_value(column, row.get(column), imported_at) for column in columns]
                    copy.write_row(values)
                    copied += 1
            cur.execute(skipped_sql)
            preexisting = int(cur.fetchone()[0])
            cur.execute(insert_sql)
            inserted = int(cur.fetchone()[0])
            cur.execute(f"SELECT count(*) FROM {table_sql}")
            after_count = int(cur.fetchone()[0])
        conn.commit()

    return UploadResult(
        csv_rows=copied,
        preexisting_rows=preexisting,
        inserted_rows=inserted,
        conflict_or_unreported_rows=copied - preexisting - inserted,
        before_count=before_count,
        after_count=after_count,
    )


def iter_csv_rows(csv_path: Path) -> Iterator[dict[str, Any]]:
    with csv_path.open("r", newline="", encoding="utf-8-sig") as file:
        yield from csv.DictReader(file)


if __name__ == "__main__":
    main()
