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
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace the existing mapping tables and review view in one transaction.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate and report the existing mapping tables without rebuilding them.",
    )
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
    if args.validate_only:
        with connector.connect() as conn:
            with conn.cursor() as cur:
                validate_mapping(cur)
        print("validation=passed")
        print_results(connector)
        return

    build_mapping(connector, args.sql_file, replace=args.replace)
    print_results(connector)


def build_mapping(connector: PostgresConnector, sql_file: Path, *, replace: bool) -> None:
    if not sql_file.is_file():
        raise FileNotFoundError(sql_file)

    existing = connector.fetch_all(
        "SELECT table_schema || '.' || table_name "
        "FROM information_schema.tables "
        "WHERE table_schema = 'propdb_staging' "
        "AND table_name IN ('data_universe_prop_id_candidates', 'data_universe_master_map')"
    )
    if existing and not replace:
        names = ", ".join(row[0] for row in existing)
        raise RuntimeError(f"Refusing to replace existing mapping tables: {names}")

    started = time.monotonic()
    with connector.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = 0")
            if replace:
                cur.execute("DROP VIEW IF EXISTS propdb_staging.data_universe_mapping_review")
                cur.execute("DROP TABLE IF EXISTS propdb_staging.data_universe_master_map")
                cur.execute("DROP TABLE IF EXISTS propdb_staging.data_universe_prop_id_candidates")
            cur.execute(sql_file.read_text(encoding="ascii"), prepare=False)
            cur.execute(
                "SELECT propdb_staging.normalise_parcel_address(%s)",
                ("1001/1 AVON RD, PYMBLE NSW 2073",),
            )
            actual = cur.fetchone()[0]
            if actual != "1 AVON ROAD PYMBLE":
                raise RuntimeError(f"Address normalisation self-check failed: {actual!r}")
            cur.execute(
                "SELECT count(*) FROM propdb_staging.data_universe_prop_id_candidates "
                "WHERE canonical_address_source = 'data_universe' "
                "AND data_universe_address_has_unit"
            )
            unit_source_violations = cur.fetchone()[0]
            if unit_source_violations:
                raise RuntimeError(
                    f"Unit-address exclusion self-check failed: {unit_source_violations} rows"
                )
            validate_mapping(cur)
        conn.commit()
    print(f"build_seconds={time.monotonic() - started:.1f}")


def validate_mapping(cur) -> None:
    cur.execute("""
        SELECT
            (SELECT count(*) FROM propdb_staging.data_universe),
            (SELECT count(*) FROM propdb_staging.data_universe_prop_id_candidates),
            (SELECT count(*) FROM propdb_staging.data_universe_master_map),
            (SELECT count(*) FROM (
                SELECT canonical_address
                FROM propdb_staging.data_universe_master_map
                WHERE primary_prop_id IS NOT NULL
                GROUP BY canonical_address
                HAVING count(DISTINCT primary_prop_id) > 1
            ) inconsistent_addresses),
            (SELECT count(*)
             FROM propdb_staging.data_universe_master_map m
             LEFT JOIN propdb_staging.data_universe_master_map p
               ON p.prop_id = m.primary_prop_id
             WHERE m.primary_prop_id IS NOT NULL
               AND (p.prop_id IS NULL OR p.canonical_address IS DISTINCT FROM m.canonical_address)),
            (SELECT count(*)
             FROM propdb_staging.data_universe_master_map
             WHERE mapping_status = 'primary_latest_recent_sale'
               AND (
                   NOT has_recent_historical_import
                   OR latest_sale_date IS DISTINCT FROM latest_recent_sale_date_at_address
               ))
    """)
    source_rows, candidate_rows, master_rows, inconsistent, broken_links, bad_step4 = cur.fetchone()
    if source_rows != candidate_rows or candidate_rows != master_rows:
        raise RuntimeError(
            "Mapping row-count self-check failed: "
            f"source={source_rows} candidate={candidate_rows} master={master_rows}"
        )
    if inconsistent or broken_links or bad_step4:
        raise RuntimeError(
            "Mapping integrity self-check failed: "
            f"inconsistent_addresses={inconsistent} broken_links={broken_links} "
            f"bad_step4_primaries={bad_step4}"
        )


def print_results(connector: PostgresConnector) -> None:
    print("mapping_status")
    for status, count in connector.fetch_all(
        "SELECT mapping_status, count(*) "
        "FROM propdb_staging.data_universe_master_map "
        "GROUP BY mapping_status ORDER BY mapping_status"
    ):
        print(f"{status}={count}")

    duplicate_result = connector.fetch_one("""
        WITH groups AS (
            SELECT
                canonical_address,
                count(*) AS prop_id_count,
                count(*) FILTER (
                    WHERE has_land_valuation AND has_historical_sale
                ) AS both_count,
                count(*) FILTER (WHERE has_land_valuation) AS land_valuation_count
            FROM propdb_staging.data_universe_prop_id_candidates
            WHERE eligible_for_primary_address
            GROUP BY canonical_address
        )
        SELECT
            (SELECT count(*) FROM propdb_staging.data_universe_prop_id_candidates
             WHERE NOT eligible_for_primary_address),
            count(*) FILTER (WHERE prop_id_count > 1),
            count(*) FILTER (WHERE both_count > 1),
            count(*) FILTER (WHERE both_count <> 1 AND land_valuation_count > 1),
            (SELECT count(DISTINCT canonical_address)
             FROM propdb_staging.data_universe_master_map
             WHERE primary_selection_rule = 'recent_historical_sales_latest_sale'),
            (SELECT count(DISTINCT canonical_address)
             FROM propdb_staging.data_universe_master_map
             WHERE mapping_status = 'review_latest_sale_date_tie')
        FROM groups
    """)
    labels = (
        "ineligible_candidate_rows",
        "duplicated_addresses_after_unit_exclusion",
        "step2_duplicate_addresses",
        "step3_duplicate_addresses_after_step2",
        "step4_resolved_addresses",
        "step4_latest_sale_date_tie_addresses",
    )
    print("primary_rule_summary")
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
