from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_CONNECTOR_SRC = Path(r"D:\projects\db_connector\src")
for path in (PROJECT_ROOT / "src", DB_CONNECTOR_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from db_connector import DatabaseConfig, PostgresConnector


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the reviewed data_universe parcel mapping layers.")
    parser.add_argument("--sql-file", type=Path, default=PROJECT_ROOT / "sql" / "build_data_universe_mapping.sql")
    parser.add_argument("--db-host", default="100.124.134.29")
    parser.add_argument("--db-port", type=int, default=5432)
    parser.add_argument("--db-name", default="banner17_master")
    parser.add_argument("--db-user", default="banner17")
    parser.add_argument("--db-password-keyring-service", default="banner17")
    parser.add_argument("--db-password-keyring-username", default="banner17")
    parser.add_argument("--db-sslmode")
    args = parser.parse_args()

    connector = PostgresConnector(
        DatabaseConfig(
            host=args.db_host,
            port=args.db_port,
            database=args.db_name,
            user=args.db_user,
            sslmode=args.db_sslmode,
            password_keyring_service=args.db_password_keyring_service,
            password_keyring_username=args.db_password_keyring_username,
        )
    )
    build_mapping(connector, args.sql_file)
    print_results(connector)


def build_mapping(connector: PostgresConnector, sql_file: Path) -> None:
    if not sql_file.is_file():
        raise FileNotFoundError(sql_file)

    existing = connector.fetch_all(
        "SELECT table_schema || '.' || table_name "
        "FROM information_schema.tables "
        "WHERE table_schema = 'propdb_staging' "
        "AND table_name IN ('data_universe_prop_id_candidates', 'data_universe_master_map')"
    )
    if existing:
        names = ", ".join(row[0] for row in existing)
        raise RuntimeError(f"Refusing to replace existing mapping tables: {names}")

    started = time.monotonic()
    with connector.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = 0")
            cur.execute(sql_file.read_text(encoding="ascii"), prepare=False)
            cur.execute(
                "SELECT propdb_staging.normalise_parcel_address(%s)",
                ("1001/1 AVON RD, PYMBLE NSW 2073",),
            )
            actual = cur.fetchone()[0]
            if actual != "1 AVON ROAD PYMBLE":
                raise RuntimeError(f"Address normalisation self-check failed: {actual!r}")
        conn.commit()
    print(f"build_seconds={time.monotonic() - started:.1f}")


def print_results(connector: PostgresConnector) -> None:
    print("mapping_status")
    for status, count in connector.fetch_all(
        "SELECT mapping_status, count(*) "
        "FROM propdb_staging.data_universe_master_map "
        "GROUP BY mapping_status ORDER BY mapping_status"
    ):
        print(f"{status}={count}")

    duplicate_result = connector.fetch_one(
        "WITH remaining AS ("
        "SELECT prop_id, propdb_staging.normalise_parcel_address(original_address) AS address "
        "FROM propdb_staging.data_universe_prop_id_candidates "
        "WHERE NOT data_universe_address_has_unit AND original_address IS NOT NULL"
        "), duplicate_addresses AS ("
        "SELECT address, count(DISTINCT prop_id) AS prop_ids "
        "FROM remaining WHERE address IS NOT NULL GROUP BY address HAVING count(DISTINCT prop_id) > 1"
        ") SELECT "
        "(SELECT count(*) FROM propdb_staging.data_universe_prop_id_candidates WHERE data_universe_address_has_unit), "
        "(SELECT count(*) FROM remaining WHERE address IS NOT NULL), "
        "count(*), COALESCE(sum(prop_ids), 0), COALESCE(sum(prop_ids - 1), 0) "
        "FROM duplicate_addresses"
    )
    labels = (
        "unit_address_rows_removed",
        "non_unit_prop_ids_remaining",
        "duplicated_addresses_remaining",
        "prop_ids_on_duplicated_addresses",
        "extra_prop_ids_on_duplicated_addresses",
    )
    print("non_unit_duplicate_summary")
    for label, value in zip(labels, duplicate_result, strict=True):
        print(f"{label}={value}")

    table_counts = connector.fetch_one(
        "SELECT "
        "(SELECT count(*) FROM propdb_staging.data_universe_prop_id_candidates), "
        "(SELECT count(*) FROM propdb_staging.data_universe_master_map), "
        "(SELECT count(*) FROM propdb_staging.data_universe_mapping_review)"
    )
    print(f"candidate_rows={table_counts[0]}")
    print(f"master_map_rows={table_counts[1]}")
    print(f"review_rows={table_counts[2]}")


if __name__ == "__main__":
    main()
