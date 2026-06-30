from __future__ import annotations

from six_nsw_property_download.db import (
    DEFAULT_PROPERTY_TABLE,
    DEFAULT_PROPID_COLUMN,
    PostgresConfig,
    PostgresPropertyIdConnector,
    build_distinct_propid_query,
    build_propid_query,
    coerce_upload_value,
)
from six_nsw_property_download.timezone import HONG_KONG_TZ


def test_build_propid_query_quotes_schema_table_and_column() -> None:
    assert (
        build_propid_query("public.property_ids", "propid")
        == 'select "propid" from "public"."property_ids" where "propid" is not null'
    )


def test_build_distinct_propid_query() -> None:
    assert (
        build_distinct_propid_query(DEFAULT_PROPERTY_TABLE, DEFAULT_PROPID_COLUMN)
        == 'select distinct "url_property_id" from "propdb_staging"."nsw_property_sales_all_history" where "url_property_id" is not null'
    )


def test_connector_defaults_to_target_table_distinct_property_ids() -> None:
    connector = PostgresPropertyIdConnector(PostgresConfig(dsn="postgresql://example"))

    assert connector.query == (
        'select distinct "url_property_id" from "propdb_staging"."nsw_property_sales_all_history" '
        'where "url_property_id" is not null'
    )


def test_postgres_config_prefers_dsn() -> None:
    config = PostgresConfig(
        host="ignored",
        port=5432,
        database="ignored",
        user="ignored",
        password="ignored",
        dsn="postgresql://user:pass@host:5432/db",
    )

    assert config.connection_info() == "postgresql://user:pass@host:5432/db"


def test_postgres_config_builds_connection_info() -> None:
    config = PostgresConfig(
        host="localhost",
        port=5432,
        database="property_db",
        user="property_user",
        password="secret value",
        sslmode="prefer",
    )

    assert config.connection_info() == "host=localhost port=5432 dbname=property_db user=property_user password='secret value' sslmode=prefer"


def test_upload_coercion_sets_imported_at() -> None:
    imported_at = object()

    assert coerce_upload_value("imported_at", "", imported_at) is imported_at
    assert coerce_upload_value("imported_at", "2020-01-01T00:00:00+00:00", imported_at) is imported_at
    assert coerce_upload_value("sale_price", "", imported_at) is None
    assert coerce_upload_value("street_name", "", imported_at) == ""


def test_hong_kong_timezone_is_used_for_dynamic_timestamps() -> None:
    assert HONG_KONG_TZ.key == "Asia/Hong_Kong"
