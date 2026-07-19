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


OLD_OBJECTS = (
    "data_universe_prop_id_candidates",
    "data_universe_master_map",
)
NEW_OBJECTS = (
    "banner_address",
    "banner_address_unit",
    "banner_address_prop_id_map",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build BAID address-to-PropID mapping tables.")
    parser.add_argument(
        "--sql-file",
        type=Path,
        default=PROJECT_ROOT / "sql" / "build_banner_address_mapping.sql",
    )
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
        help="Drop the old primary-mapping objects and replace existing BAID tables atomically.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate and report existing BAID tables without rebuilding them.",
    )
    args = parser.parse_args()

    connector = PostgresConnector(DatabaseConfig(
        host=args.db_host,
        port=args.db_port,
        database=args.db_name,
        user=args.db_user,
        sslmode=args.db_sslmode,
        password_keyring_service=args.db_password_keyring_service,
        password_keyring_username=args.db_password_keyring_username,
    ))

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
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'propdb_staging' "
        "AND table_name = ANY(%s) ORDER BY table_name",
        (list(OLD_OBJECTS + NEW_OBJECTS),),
    )
    if existing and not replace:
        names = ", ".join(row[0] for row in existing)
        raise RuntimeError(f"Refusing to replace existing mapping objects: {names}")

    started = time.monotonic()
    with connector.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = 0")
            cur.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            if replace:
                cur.execute("DROP VIEW IF EXISTS propdb_staging.data_universe_mapping_review")
                cur.execute("DROP TABLE IF EXISTS propdb_staging.data_universe_master_map")
                cur.execute("DROP TABLE IF EXISTS propdb_staging.data_universe_prop_id_candidates")
                cur.execute("DROP TABLE IF EXISTS propdb_staging.banner_address_prop_id_map")
                cur.execute("DROP TABLE IF EXISTS propdb_staging.banner_address_unit")
                cur.execute("DROP TABLE IF EXISTS propdb_staging.banner_address")
            cur.execute(sql_file.read_text(encoding="ascii"), prepare=False)
            validate_mapping(cur)
        conn.commit()
    print(f"build_seconds={time.monotonic() - started:.1f}")


def validate_mapping(cur) -> None:
    cur.execute(
        "SELECT propdb_staging.normalise_banner_base_address(%s), "
        "propdb_staging.normalise_banner_address(%s)",
        (
            "101/1 AVON RD, PYMBLE NSW 2073",
            "101/1 AVON RD, PYMBLE NSW 2073",
        ),
    )
    base_address, unit_address = cur.fetchone()
    if base_address != "1 AVON ROAD PYMBLE NSW 2073":
        raise RuntimeError(f"Base-address normalisation failed: {base_address!r}")
    if unit_address != "101/1 AVON ROAD PYMBLE NSW 2073":
        raise RuntimeError(f"Unit-address normalisation failed: {unit_address!r}")

    cur.execute("""
        SELECT
            (SELECT count(*) FROM propdb_staging.data_universe
             WHERE address IS NOT NULL AND btrim(address) <> ''),
            (SELECT count(*) FROM propdb_staging.banner_address_prop_id_map),
            (SELECT count(*) FROM propdb_staging.banner_address_unit u
             LEFT JOIN propdb_staging.banner_address a USING (baid)
             WHERE a.baid IS NULL),
            (SELECT count(*) FROM propdb_staging.banner_address_prop_id_map m
             LEFT JOIN propdb_staging.banner_address a USING (baid)
             WHERE a.baid IS NULL),
            (SELECT count(*) FROM propdb_staging.banner_address_prop_id_map m
             LEFT JOIN propdb_staging.banner_address_unit u USING (unit_address_id)
             WHERE m.has_unit_number AND u.unit_address_id IS NULL),
            (SELECT count(*) FROM information_schema.columns
             WHERE table_schema = 'propdb_staging'
               AND table_name = ANY(%s)
               AND column_name IN ('primary_prop_id', 'mapping_status', 'obsolete_candidate'))
    """, (list(NEW_OBJECTS),))
    source_rows, map_rows, orphan_units, orphan_maps, missing_unit_links, old_columns = cur.fetchone()
    if source_rows != map_rows:
        raise RuntimeError(f"Source/map row mismatch: source={source_rows} map={map_rows}")
    if orphan_units or orphan_maps or missing_unit_links or old_columns:
        raise RuntimeError(
            "BAID integrity check failed: "
            f"orphan_units={orphan_units} orphan_maps={orphan_maps} "
            f"missing_unit_links={missing_unit_links} old_columns={old_columns}"
        )


def print_results(connector: PostgresConnector) -> None:
    counts = connector.fetch_one("""
        SELECT
            (SELECT count(*) FROM propdb_staging.banner_address),
            (SELECT count(*) FROM propdb_staging.banner_address_unit),
            (SELECT count(*) FROM propdb_staging.banner_address_prop_id_map),
            (SELECT count(*) FROM propdb_staging.banner_address WHERE prop_id_count > 1),
            (SELECT count(*) FROM propdb_staging.banner_address WHERE has_unit_addresses)
    """)
    labels = (
        "banner_addresses",
        "unit_addresses",
        "prop_id_map_rows",
        "addresses_with_multiple_prop_ids",
        "base_addresses_with_units",
    )
    for label, value in zip(labels, counts, strict=True):
        print(f"{label}={value}")

    print("examples")
    for row in connector.fetch_all("""
        SELECT a.baid, a.normalized_address,
               a.prop_id_count,
               a.unit_address_count,
               bool_or(m.prop_id = 4158520) AS has_prop_id_4158520,
               bool_or(m.prop_id = 4161744) AS has_prop_id_4161744,
               bool_or(m.prop_id = 4171107) AS has_prop_id_4171107,
               ARRAY(
                   SELECT child.normalized_unit_address
                   FROM propdb_staging.banner_address_unit child
                   WHERE child.baid = a.baid
                   ORDER BY child.normalized_unit_address
                   LIMIT 5
               ) AS unit_address_samples
        FROM propdb_staging.banner_address a
        JOIN propdb_staging.banner_address_prop_id_map m USING (baid)
        WHERE a.normalized_address IN (
            '1 AVON ROAD PYMBLE NSW 2073',
            '150 PACIFIC HIGHWAY NORTH SYDNEY NSW 2060'
        )
        GROUP BY a.baid, a.normalized_address, a.prop_id_count, a.unit_address_count
        ORDER BY a.normalized_address
    """):
        print(row)


if __name__ == "__main__":
    main()
