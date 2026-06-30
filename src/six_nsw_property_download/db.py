from __future__ import annotations

import os
import re
import logging
from collections.abc import Iterable
from collections.abc import Iterator
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .timezone import HONG_KONG_TZ
from .transform import OUTPUT_COLUMNS


logger = logging.getLogger(__name__)

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_PROPERTY_TABLE = "propdb_staging.nsw_property_sales_all_history"
DEFAULT_PROPID_COLUMN = "url_property_id"
DEFAULT_KEYRING_SERVICE = "banner17"
UPLOAD_COLUMNS = [column for column in OUTPUT_COLUMNS if column != "id"]
NULLABLE_CAST_COLUMNS = {
    "url_property_id",
    "sale_price",
    "sale_date",
    "area_sqm",
    "is_multi_property_sale",
    "property_number",
    "extraction_date",
    "downloaded_at",
    "imported_at",
}


@dataclass(frozen=True)
class PostgresConfig:
    host: str = "localhost"
    port: int = 5432
    database: str = ""
    user: str = ""
    password: str = ""
    sslmode: str | None = None
    dsn: str | None = None
    password_keyring_service: str | None = DEFAULT_KEYRING_SERVICE
    password_keyring_username: str | None = None

    @classmethod
    def from_env(cls, prefix: str = "PG") -> "PostgresConfig":
        return cls(
            host=os.getenv(f"{prefix}HOST", "localhost"),
            port=int(os.getenv(f"{prefix}PORT", "5432")),
            database=os.getenv(f"{prefix}DATABASE", os.getenv(f"{prefix}DB", "")),
            user=os.getenv(f"{prefix}USER", ""),
            password=os.getenv(f"{prefix}PASSWORD", ""),
            sslmode=os.getenv(f"{prefix}SSLMODE") or None,
            dsn=os.getenv(f"{prefix}DSN") or None,
            password_keyring_service=os.getenv(f"{prefix}PASSWORD_KEYRING_SERVICE", DEFAULT_KEYRING_SERVICE),
            password_keyring_username=os.getenv(f"{prefix}PASSWORD_KEYRING_USERNAME") or None,
        )

    def connection_info(self) -> str:
        if self.dsn:
            return self.dsn

        password = self.resolved_password()

        info = {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "password": password,
        }
        if self.sslmode:
            info["sslmode"] = self.sslmode
        return " ".join(f"{key}={quote_conninfo_value(value)}" for key, value in info.items() if value != "")

    def resolved_password(self) -> str:
        if self.password:
            return self.password
        if not self.password_keyring_service:
            return ""

        username = self.password_keyring_username or self.user
        if not username:
            raise ValueError("A database user is required to read the password from keyring.")

        try:
            import keyring
        except ImportError as exc:
            raise RuntimeError("keyring is required to read the database password from Windows Credential Manager.") from exc

        password = keyring.get_password(self.password_keyring_service, username)
        if password is not None:
            return password

        password = read_windows_generic_credential(self.password_keyring_service)
        if password is not None:
            return password

        raise RuntimeError(
            f"No password found in keyring for service '{self.password_keyring_service}' and username '{username}', "
            f"or as a Windows generic credential named '{self.password_keyring_service}'."
        )


class PostgresPropertyIdConnector:
    def __init__(
        self,
        config: PostgresConfig,
        *,
        propid_table: str | None = DEFAULT_PROPERTY_TABLE,
        propid_column: str = DEFAULT_PROPID_COLUMN,
        propid_query: str | None = None,
        batch_size: int = 10_000,
        distinct: bool = True,
    ) -> None:
        self.config = config
        self.propid_table = propid_table
        self.propid_column = propid_column
        self.propid_query = propid_query
        self.batch_size = batch_size
        self.distinct = distinct

    @property
    def query(self) -> str:
        if self.propid_query:
            return self.propid_query
        if not self.propid_table:
            raise ValueError("A propid table or custom propid query is required.")
        return _build_propid_query(self.propid_table, self.propid_column, distinct=self.distinct)

    def test_connection(self) -> str:
        import psycopg

        logger.info("db_test_connection_start host=%s database=%s user=%s", self.config.host, self.config.database, self.config.user)
        with psycopg.connect(self.config.connection_info()) as conn:
            with conn.cursor() as cur:
                cur.execute("select version()")
                version = str(cur.fetchone()[0])
                logger.info("db_test_connection_success version=%s", version)
                return version

    def preview_propids(self, limit: int = 10) -> list[int]:
        preview_query = f"select propid from ({self.query}) as source_propids(propid) limit %s"
        import psycopg

        logger.info("db_preview_propids_start limit=%s query=%s", limit, self.query)
        with psycopg.connect(self.config.connection_info()) as conn:
            with conn.cursor() as cur:
                cur.execute(preview_query, (limit,))
                propids = [int(row[0]) for row in cur.fetchall() if row[0] is not None]
                logger.info("db_preview_propids_success count=%s propids=%s", len(propids), propids)
                return propids

    def iter_propids(self) -> Iterator[int]:
        import psycopg

        logger.info("db_iter_propids_start batch_size=%s query=%s", self.batch_size, self.query)
        count = 0
        with psycopg.connect(self.config.connection_info()) as conn:
            with conn.cursor(name="six_property_propids") as cur:
                cur.itersize = self.batch_size
                cur.execute(self.query)
                for row in cur:
                    if row[0] is not None:
                        count += 1
                        if count <= 10 or count % 10000 == 0:
                            logger.info("db_iter_propids_yield count=%s propid=%s", count, row[0])
                        yield int(row[0])
        logger.info("db_iter_propids_complete count=%s", count)


def build_propid_query(table: str, column: str) -> str:
    return _build_propid_query(table, column, distinct=False)


def build_distinct_propid_query(table: str, column: str) -> str:
    return _build_propid_query(table, column, distinct=True)


def _build_propid_query(table: str, column: str, *, distinct: bool) -> str:
    if not is_qualified_identifier(table):
        raise ValueError("Table name contains unsupported characters.")
    if not is_identifier(column):
        raise ValueError("Column name contains unsupported characters.")
    quoted_table = ".".join(quote_identifier(part) for part in table.split("."))
    quoted_column = quote_identifier(column)
    distinct_sql = "distinct " if distinct else ""
    return f"select {distinct_sql}{quoted_column} from {quoted_table} where {quoted_column} is not null"


def iter_propids(
    dsn: str,
    *,
    query: str,
    batch_size: int = 10_000,
) -> Iterator[int]:
    """Stream propids from PostgreSQL using a server-side cursor."""
    connector = PostgresPropertyIdConnector(PostgresConfig(dsn=dsn), propid_query=query, batch_size=batch_size)
    yield from connector.iter_propids()


def upload_normalized_csv(
    config: PostgresConfig,
    csv_path: Path,
    *,
    target_table: str = DEFAULT_PROPERTY_TABLE,
    columns: Iterable[str] = UPLOAD_COLUMNS,
) -> int:
    import csv

    def iter_csv_rows() -> Iterator[dict[str, object]]:
        with csv_path.open("r", newline="", encoding="utf-8") as file:
            yield from csv.DictReader(file)

    return upload_normalized_rows(config, iter_csv_rows(), target_table=target_table, columns=columns)


def upload_normalized_rows(
    config: PostgresConfig,
    rows: Iterable[dict[str, object]],
    *,
    target_table: str = DEFAULT_PROPERTY_TABLE,
    columns: Iterable[str] = UPLOAD_COLUMNS,
) -> int:
    import psycopg

    insert_columns = list(columns)
    for column in insert_columns:
        if column not in OUTPUT_COLUMNS:
            raise ValueError(f"Unknown output column for upload: {column}")

    table_sql = quote_qualified_identifier(target_table)
    temp_table_sql = quote_identifier("six_property_upload_temp")
    columns_sql = ", ".join(quote_identifier(column) for column in insert_columns)
    copy_sql = f"COPY {temp_table_sql} ({columns_sql}) FROM STDIN"
    insert_sql = (
        f"INSERT INTO {table_sql} ({columns_sql}) "
        f"SELECT {columns_sql} FROM {temp_table_sql} "
        "ON CONFLICT DO NOTHING"
    )

    copied = 0
    imported_at = datetime.now(HONG_KONG_TZ)
    logger.info("upload_start target_table=%s imported_at=%s", target_table, imported_at.isoformat())
    with psycopg.connect(config.connection_info()) as conn:
        with conn.cursor() as cur:
            logger.info("upload_create_temp_table target_table=%s temp_table=six_property_upload_temp", target_table)
            cur.execute(f"CREATE TEMP TABLE {temp_table_sql} (LIKE {table_sql} INCLUDING DEFAULTS) ON COMMIT DROP")
            logger.info("upload_copy_start target_table=%s", target_table)
            with cur.copy(copy_sql) as copy:
                for row in rows:
                    values = [coerce_upload_value(column, row.get(column), imported_at) for column in insert_columns]
                    copy.write_row(values)
                    copied += 1
                    if copied <= 10 or copied % 10000 == 0:
                        logger.info(
                            "upload_copy_row copied=%s url_property_id=%s property_number=%s sale_date=%s sale_price=%s",
                            copied,
                            row.get("url_property_id"),
                            row.get("property_number"),
                            row.get("sale_date"),
                            row.get("sale_price"),
                        )
            if copied == 0:
                logger.info("upload_no_rows target_table=%s", target_table)
                conn.commit()
                return 0
            logger.info("upload_insert_start target_table=%s copied_rows=%s conflict_policy=do_nothing", target_table, copied)
            cur.execute(insert_sql)
            inserted = cur.rowcount
        conn.commit()
    logger.info("upload_complete target_table=%s copied_rows=%s inserted_rows=%s skipped_duplicates=%s", target_table, copied, inserted, copied - inserted)
    return inserted


@dataclass(frozen=True)
class UploadRowsResult:
    copied_rows: int
    inserted_rows: int
    skipped_rows: list[dict[str, Any]]


def upload_dat_rows_with_skip_report(
    config: PostgresConfig,
    rows: Iterable[dict[str, object]],
    *,
    target_table: str = DEFAULT_PROPERTY_TABLE,
    columns: Iterable[str] = UPLOAD_COLUMNS,
) -> UploadRowsResult:
    import psycopg

    insert_columns = list(columns)
    for column in insert_columns:
        if column not in OUTPUT_COLUMNS:
            raise ValueError(f"Unknown output column for upload: {column}")

    table_sql = quote_qualified_identifier(target_table)
    temp_table_sql = quote_identifier("six_weekly_dat_upload_temp")
    columns_sql = ", ".join(quote_identifier(column) for column in insert_columns)
    prefixed_columns_sql = ", ".join(f"s.{quote_identifier(column)}" for column in insert_columns)
    copy_sql = f"COPY {temp_table_sql} ({columns_sql}) FROM STDIN"
    dat_duplicate_predicate = (
        "t.property_number IS NULL "
        "AND t.url_property_id IS NOT DISTINCT FROM s.url_property_id "
        "AND t.sale_date IS NOT DISTINCT FROM s.sale_date "
        "AND t.sale_price IS NOT DISTINCT FROM s.sale_price "
        "AND COALESCE(t.dealing_number, '') = COALESCE(s.dealing_number, '')"
    )
    skipped_sql = (
        f"SELECT {columns_sql} "
        f"FROM {temp_table_sql} s "
        f"WHERE EXISTS (SELECT 1 FROM {table_sql} t WHERE {dat_duplicate_predicate})"
    )
    insert_sql = (
        "WITH candidates AS ("
        f"SELECT {prefixed_columns_sql} "
        f"FROM {temp_table_sql} s "
        f"WHERE NOT EXISTS (SELECT 1 FROM {table_sql} t WHERE {dat_duplicate_predicate})"
        "), inserted AS ("
        f"INSERT INTO {table_sql} ({columns_sql}) "
        f"SELECT {columns_sql} FROM candidates "
        "ON CONFLICT DO NOTHING "
        "RETURNING 1"
        ") SELECT count(*) FROM inserted"
    )

    copied = 0
    imported_at = datetime.now(HONG_KONG_TZ)
    logger.info("weekly_dat_upload_start target_table=%s imported_at=%s", target_table, imported_at.isoformat())
    with psycopg.connect(config.connection_info()) as conn:
        with conn.cursor() as cur:
            logger.info("weekly_dat_upload_create_temp target_table=%s temp_table=six_weekly_dat_upload_temp", target_table)
            cur.execute(f"CREATE TEMP TABLE {temp_table_sql} (LIKE {table_sql} INCLUDING DEFAULTS) ON COMMIT DROP")
            logger.info("weekly_dat_upload_copy_start target_table=%s", target_table)
            with cur.copy(copy_sql) as copy:
                for row in rows:
                    values = [coerce_upload_value(column, row.get(column), imported_at) for column in insert_columns]
                    copy.write_row(values)
                    copied += 1
                    if copied <= 10 or copied % 10000 == 0:
                        logger.info(
                            "weekly_dat_upload_copy_row copied=%s url_property_id=%s sale_date=%s sale_price=%s dealing_number=%s",
                            copied,
                            row.get("url_property_id"),
                            row.get("sale_date"),
                            row.get("sale_price"),
                            row.get("dealing_number"),
                        )
            if copied == 0:
                conn.commit()
                return UploadRowsResult(copied_rows=0, inserted_rows=0, skipped_rows=[])

            logger.info("weekly_dat_upload_find_existing_duplicates copied_rows=%s", copied)
            cur.execute(skipped_sql)
            skipped_rows = [dict(zip(insert_columns, row, strict=True)) for row in cur.fetchall()]
            for skipped in skipped_rows:
                skipped["skip_reason"] = "duplicate_existing_dat_key"

            logger.info(
                "weekly_dat_upload_insert_start copied_rows=%s preexisting_duplicate_rows=%s conflict_policy=do_nothing",
                copied,
                len(skipped_rows),
            )
            cur.execute(insert_sql)
            inserted = int(cur.fetchone()[0])
        conn.commit()

    logger.info(
        "weekly_dat_upload_complete target_table=%s copied_rows=%s inserted_rows=%s skipped_rows=%s conflict_or_unreported_rows=%s",
        target_table,
        copied,
        inserted,
        len(skipped_rows),
        copied - inserted - len(skipped_rows),
    )
    return UploadRowsResult(copied_rows=copied, inserted_rows=inserted, skipped_rows=skipped_rows)


def coerce_upload_value(column: str, value: object, imported_at: datetime) -> object:
    if column == "imported_at":
        return imported_at
    if value == "" and column in NULLABLE_CAST_COLUMNS:
        return None
    return value


def is_qualified_identifier(value: str) -> bool:
    return all(is_identifier(part) for part in value.split("."))


def is_identifier(value: str) -> bool:
    return bool(IDENTIFIER_RE.match(value))


def quote_identifier(value: str) -> str:
    if not is_identifier(value):
        raise ValueError(f"Invalid SQL identifier: {value}")
    return f'"{value}"'


def quote_qualified_identifier(value: str) -> str:
    if not is_qualified_identifier(value):
        raise ValueError(f"Invalid SQL identifier: {value}")
    return ".".join(quote_identifier(part) for part in value.split("."))


def quote_conninfo_value(value: object) -> str:
    text = str(value)
    if not text:
        return "''"
    if re.search(r"\s|'", text):
        return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return text


def read_windows_generic_credential(target_name: str) -> str | None:
    try:
        from win32ctypes.pywin32 import pywintypes, win32cred
    except ImportError:
        return None

    try:
        credential = win32cred.CredRead(target_name, win32cred.CRED_TYPE_GENERIC)
    except pywintypes.error:
        return None

    blob = credential.get("CredentialBlob")
    if not blob:
        return None
    if isinstance(blob, str):
        return blob

    for encoding in ("utf-16-le", "utf-8"):
        try:
            return bytes(blob).decode(encoding).rstrip("\x00")
        except UnicodeDecodeError:
            continue
    return None
