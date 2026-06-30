from __future__ import annotations

import csv
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Any

from .timezone import HONG_KONG_TZ


OUTPUT_COLUMNS = [
    "id",
    "url_property_id",
    "address",
    "sale_price",
    "sale_date",
    "area_sqm",
    "strata_type",
    "is_multi_property_sale",
    "property_number",
    "dealing_number",
    "extraction_date",
    "unit_num",
    "house_num",
    "street_name",
    "suburb",
    "state",
    "postcode",
    "source_file",
    "downloaded_at",
    "imported_at",
]

SOURCE_COLUMNS = {
    "ADDRESS",
    "SALE PRICE",
    "SALE DATE",
    "AREA",
    "STRATA/NON STRATA",
    "MULTI-PROPERTY SALE (Y/N)",
    "PROPERTY NUMBER",
    "DEALING NUMBER",
    "EXTRACTION DATE",
}

DATE_FORMATS = ("%d %B %Y", "%d %b %Y", "%Y-%m-%d")
LOCALITY_RE = re.compile(r"^(?P<suburb>.+?)\s+(?P<state>[A-Z]{2,3})\s+(?P<postcode>\d{4})$")
SLASH_UNIT_RE = re.compile(
    r"^(?P<unit>[A-Za-z0-9-]+)\s*/\s*(?P<house>\d+[A-Za-z]?(?:-\d+[A-Za-z]?)?)\s+(?P<street>.+)$"
)
LABELLED_UNIT_RE = re.compile(
    r"^(?:UNIT|U|SHOP|SUITE|LOT|LVL|LEVEL|FLAT|APT|APARTMENT)\s+"
    r"(?P<unit>[A-Za-z0-9-]+)\s+"
    r"(?P<house>\d+[A-Za-z]?(?:-\d+[A-Za-z]?)?)\s+"
    r"(?P<street>.+)$",
    re.IGNORECASE,
)
HOUSE_RE = re.compile(r"^(?P<house>\d+[A-Za-z]?(?:-\d+[A-Za-z]?)?)\s+(?P<street>.+)$")


def parse_source_csv(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(StringIO(text.replace("\r\n", "\n")))
    missing = SOURCE_COLUMNS.difference(reader.fieldnames or [])
    if missing:
        raise ValueError(f"Source CSV is missing columns: {', '.join(sorted(missing))}")
    return [dict(row) for row in reader]


def normalize_rows(csv_text: str, *, source_file: str, downloaded_at: datetime | None = None) -> list[dict[str, Any]]:
    downloaded_at = downloaded_at or datetime.now(HONG_KONG_TZ)
    return [
        normalize_row(row, source_file=source_file, downloaded_at=downloaded_at)
        for row in parse_source_csv(csv_text)
    ]


def normalize_row(row: dict[str, str], *, source_file: str, downloaded_at: datetime) -> dict[str, Any]:
    address_parts = parse_address(row.get("ADDRESS", ""))
    property_number = parse_int(row.get("PROPERTY NUMBER"))

    return {
        "id": "",
        "url_property_id": property_number,
        "address": clean_text(row.get("ADDRESS")),
        "sale_price": parse_decimal(row.get("SALE PRICE")),
        "sale_date": parse_date(row.get("SALE DATE")),
        "area_sqm": parse_decimal(row.get("AREA")),
        "strata_type": clean_text(row.get("STRATA/NON STRATA")),
        "is_multi_property_sale": parse_yes_no(row.get("MULTI-PROPERTY SALE (Y/N)")),
        "property_number": property_number,
        "dealing_number": clean_text(row.get("DEALING NUMBER")),
        "extraction_date": parse_date(row.get("EXTRACTION DATE")),
        "unit_num": address_parts["unit_num"],
        "house_num": address_parts["house_num"],
        "street_name": address_parts["street_name"],
        "suburb": address_parts["suburb"],
        "state": address_parts["state"],
        "postcode": address_parts["postcode"],
        "source_file": source_file,
        "downloaded_at": downloaded_at.isoformat(),
        "imported_at": "",
    }


def parse_address(address: str | None) -> dict[str, str]:
    address = clean_text(address)
    street_part, locality_part = split_address(address)
    unit_num = ""
    house_num = ""
    street_name = street_part

    for pattern in (SLASH_UNIT_RE, LABELLED_UNIT_RE):
        match = pattern.match(street_part)
        if match:
            unit_num = match.group("unit")
            house_num = match.group("house")
            street_name = clean_text(match.group("street"))
            break
    else:
        match = HOUSE_RE.match(street_part)
        if match:
            house_num = match.group("house")
            street_name = clean_text(match.group("street"))

    suburb = ""
    state = ""
    postcode = ""
    locality_match = LOCALITY_RE.match(locality_part)
    if locality_match:
        suburb = clean_text(locality_match.group("suburb"))
        state = locality_match.group("state")
        postcode = locality_match.group("postcode")

    return {
        "unit_num": unit_num,
        "house_num": house_num,
        "street_name": street_name,
        "suburb": suburb,
        "state": state,
        "postcode": postcode,
    }


def split_address(address: str) -> tuple[str, str]:
    if "," not in address:
        return address, ""
    street_part, locality_part = address.rsplit(",", 1)
    return clean_text(street_part), clean_text(locality_part)


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def parse_int(value: str | None) -> int | None:
    text = clean_text(value).replace(",", "")
    return int(text) if text else None


def parse_decimal(value: str | None) -> Decimal | None:
    text = clean_text(value).replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid decimal value: {value}") from exc


def parse_date(value: str | None) -> str:
    text = clean_text(value)
    if not text:
        return ""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"Invalid date value: {value}")


def parse_yes_no(value: str | None) -> bool | None:
    text = clean_text(value).lower()
    if text in {"yes", "y", "true", "1"}:
        return True
    if text in {"no", "n", "false", "0"}:
        return False
    return None
