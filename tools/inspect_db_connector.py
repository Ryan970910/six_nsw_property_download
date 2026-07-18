from __future__ import annotations

import sys
from pathlib import Path


DB_CONNECTOR_SRC = Path(r"D:\projects\db_connector\src")
if str(DB_CONNECTOR_SRC) not in sys.path:
    sys.path.insert(0, str(DB_CONNECTOR_SRC))

from db_connector import DatabaseConfig, PostgresConnector


def make_connector(database: str = "postgres") -> PostgresConnector:
    return PostgresConnector(
        DatabaseConfig(
            host="100.124.134.29",
            port=5432,
            database=database,
            user="banner17",
            password_keyring_service="banner17",
            password_keyring_username="banner17",
        )
    )


def main() -> None:
    connector = make_connector()
    databases = [
        row[0]
        for row in connector.fetch_all(
            "select datname from pg_database where datallowconn and not datistemplate order by datname"
        )
    ]
    print("databases=" + ",".join(databases))

    for database in databases:
        try:
            db = make_connector(database)
            matches = db.fetch_all(
                """
                select table_schema, table_name
                from information_schema.tables
                where table_schema = 'propdb_staging'
                  and table_name = 'nsw_property_sales_all_history'
                """
            )
            if matches:
                print(f"target_found={database}:{matches}")
        except Exception as exc:
            print(f"target_check_failed={database}:{exc}")


if __name__ == "__main__":
    main()
