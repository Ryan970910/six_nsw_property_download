from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from six_nsw_property_download.transform import normalize_rows, parse_address
from six_nsw_property_download.timezone import HONG_KONG_TZ
from six_nsw_property_download.urls import property_csv_url


SAMPLE_CSV = """ADDRESS,SALE PRICE,SALE DATE,AREA,STRATA/NON STRATA,MULTI-PROPERTY SALE (Y/N),PROPERTY NUMBER,DEALING NUMBER,EXTRACTION DATE
"317 BLACK SWAMP ROAD, TENTERFIELD NSW 2372","80000","3 April 2004","192500","NON STRATA","No",3113491,"AA759332","31 May 2026"
"""


def test_property_csv_url_matches_six_html_formula() -> None:
    assert property_csv_url(3113491) == "http://maps.six.nsw.gov.au/csv/current/property/03113000/3113491.csv"


def test_normalize_rows_matches_target_schema_and_types() -> None:
    rows = normalize_rows(
        SAMPLE_CSV,
        source_file="sample.csv",
        downloaded_at=datetime(2026, 6, 21, 14, 50, tzinfo=UTC),
    )

    assert rows == [
        {
            "id": "",
            "url_property_id": 3113491,
            "address": "317 BLACK SWAMP ROAD, TENTERFIELD NSW 2372",
            "sale_price": Decimal("80000"),
            "sale_date": "2004-04-03",
            "area_sqm": Decimal("192500"),
            "strata_type": "NON STRATA",
            "is_multi_property_sale": False,
            "property_number": 3113491,
            "dealing_number": "AA759332",
            "extraction_date": "2026-05-31",
            "unit_num": "",
            "house_num": "317",
            "street_name": "BLACK SWAMP ROAD",
            "suburb": "TENTERFIELD",
            "state": "NSW",
            "postcode": "2372",
            "source_file": "sample.csv",
            "downloaded_at": "2026-06-21T14:50:00+00:00",
            "imported_at": "",
        }
    ]


def test_normalize_rows_default_downloaded_at_uses_hong_kong_time() -> None:
    row = normalize_rows(SAMPLE_CSV, source_file="sample.csv")[0]

    assert row["downloaded_at"].endswith("+08:00")
    assert datetime.fromisoformat(row["downloaded_at"]).tzinfo is not None
    assert HONG_KONG_TZ.key == "Asia/Hong_Kong"


def test_parse_address_with_unit_slash() -> None:
    assert parse_address("2/18 SAMPLE STREET, SYDNEY NSW 2000") == {
        "unit_num": "2",
        "house_num": "18",
        "street_name": "SAMPLE STREET",
        "suburb": "SYDNEY",
        "state": "NSW",
        "postcode": "2000",
    }


def test_parse_address_with_labelled_unit() -> None:
    assert parse_address("UNIT 5 10 MAIN ROAD, NEWCASTLE NSW 2300") == {
        "unit_num": "5",
        "house_num": "10",
        "street_name": "MAIN ROAD",
        "suburb": "NEWCASTLE",
        "state": "NSW",
        "postcode": "2300",
    }
