from __future__ import annotations

import logging
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .timezone import HONG_KONG_TZ
from .transform import OUTPUT_COLUMNS, clean_text, parse_yes_no


logger = logging.getLogger(__name__)

WEEKLY_PAGE_URL = "https://valuation.property.nsw.gov.au/embed/propertySalesInformation"
WEEKLY_ZIP_URL_TEMPLATE = "https://www.valuergeneral.nsw.gov.au/__psi/weekly/{yyyymmdd}.zip"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass(frozen=True)
class WeeklyZip:
    week: date
    url: str
    content: bytes


@dataclass(frozen=True)
class DatParseResult:
    rows: list[dict[str, Any]]
    raw_b_records: list[dict[str, str]]
    files: int


def discover_weekly_zip_dates() -> list[date]:
    logger.info("weekly_discovery_start page=%s", WEEKLY_PAGE_URL)
    html = _http_get(WEEKLY_PAGE_URL).decode("utf-8", errors="replace")
    dates: set[date] = set()

    for match in re.finditer(r"/__psi/weekly/(\d{8})\.zip", html, flags=re.IGNORECASE):
        dates.add(_parse_yyyymmdd(match.group(1)))

    for match in re.finditer(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\b", html):
        try:
            dates.add(datetime.strptime(" ".join(match.groups()), "%d %b %Y").date())
        except ValueError:
            try:
                dates.add(datetime.strptime(" ".join(match.groups()), "%d %B %Y").date())
            except ValueError:
                pass

    ordered = sorted(dates)
    logger.info("weekly_discovery_complete count=%s dates=%s", len(ordered), [d.isoformat() for d in ordered])
    return ordered


def latest_weekly_zip_date() -> date:
    dates = discover_weekly_zip_dates()
    if not dates:
        raise RuntimeError(f"No weekly ZIP links were found on {WEEKLY_PAGE_URL}.")
    return dates[-1]


def download_weekly_zip(week: date, *, output_dir: Path | None = None) -> WeeklyZip:
    yyyymmdd = week.strftime("%Y%m%d")
    url = WEEKLY_ZIP_URL_TEMPLATE.format(yyyymmdd=yyyymmdd)
    if output_dir:
        zip_path = output_dir / f"{yyyymmdd}.zip"
        if zip_path.exists():
            content = zip_path.read_bytes()
            if not zipfile.is_zipfile(BytesIO(content)):
                raise RuntimeError(f"Cached weekly file is not a ZIP file: {zip_path}")
            logger.info("weekly_download_cache_hit week=%s path=%s bytes=%s", week.isoformat(), zip_path, len(content))
            return WeeklyZip(week=week, url=url, content=content)

    logger.info("weekly_download_start week=%s url=%s", week.isoformat(), url)
    content = _http_get(url)
    if not zipfile.is_zipfile(BytesIO(content)):
        raise RuntimeError(f"Downloaded content for {week.isoformat()} is not a ZIP file.")
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        zip_path = output_dir / f"{yyyymmdd}.zip"
        zip_path.write_bytes(content)
        logger.info("weekly_download_saved week=%s path=%s bytes=%s", week.isoformat(), zip_path, len(content))
    logger.info("weekly_download_complete week=%s bytes=%s", week.isoformat(), len(content))
    return WeeklyZip(week=week, url=url, content=content)


def parse_weekly_zip(weekly_zip: WeeklyZip) -> DatParseResult:
    logger.info("weekly_parse_zip_start week=%s", weekly_zip.week.isoformat())
    rows: list[dict[str, Any]] = []
    raw_b_records: list[dict[str, str]] = []
    file_count = 0
    downloaded_at = datetime.now(HONG_KONG_TZ)

    with zipfile.ZipFile(BytesIO(weekly_zip.content)) as archive:
        dat_names = [name for name in archive.namelist() if name.upper().endswith(".DAT")]
        logger.info("weekly_parse_zip_files week=%s dat_files=%s", weekly_zip.week.isoformat(), len(dat_names))
        for name in dat_names:
            file_count += 1
            text = archive.read(name).decode("utf-8-sig", errors="replace")
            parsed_rows, parsed_raw = parse_dat_text(
                text,
                source_file=f"{weekly_zip.url}#{Path(name).name}",
                downloaded_at=downloaded_at,
            )
            rows.extend(parsed_rows)
            raw_b_records.extend(parsed_raw)
            logger.info(
                "weekly_parse_file_complete week=%s file=%s rows=%s cumulative_rows=%s",
                weekly_zip.week.isoformat(),
                name,
                len(parsed_rows),
                len(rows),
            )

    logger.info(
        "weekly_parse_zip_complete week=%s files=%s rows=%s",
        weekly_zip.week.isoformat(),
        file_count,
        len(rows),
    )
    return DatParseResult(rows=rows, raw_b_records=raw_b_records, files=file_count)


def parse_dat_text(text: str, *, source_file: str, downloaded_at: datetime) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    rows: list[dict[str, Any]] = []
    raw_records: list[dict[str, str]] = []
    title_refs_by_key: dict[tuple[str, str, str], list[str]] = {}

    lines = [line for line in text.splitlines() if line.strip()]
    split_lines = [line.split(";") for line in lines]

    for fields in split_lines:
        if fields[0] != "C":
            continue
        key = _record_key(fields)
        title_ref = _field(fields, 5)
        if title_ref:
            title_refs_by_key.setdefault(key, []).append(title_ref)

    for fields in split_lines:
        if fields[0] != "B":
            continue
        row, raw = parse_b_record(fields, source_file=source_file, downloaded_at=downloaded_at)
        title_refs = title_refs_by_key.get(_record_key(fields), [])
        if title_refs and not row["dealing_number"]:
            row["dealing_number"] = title_refs[0]
        raw["title_references"] = "|".join(title_refs)
        rows.append(row)
        raw_records.append(raw)

    return rows, raw_records


def parse_b_record(fields: list[str], *, source_file: str, downloaded_at: datetime) -> tuple[dict[str, Any], dict[str, str]]:
    district_code = _field(fields, 1)
    property_id = _parse_int(_field(fields, 2))
    sale_record_number = _field(fields, 3)
    extraction_date = _parse_dat_datetime_date(_field(fields, 4))
    unit_num = clean_text(_field(fields, 5))
    house_num = clean_text(" ".join(part for part in [_field(fields, 7), _field(fields, 6)] if part))
    street_name = clean_text(_field(fields, 8))
    suburb = clean_text(_field(fields, 9))
    postcode = clean_text(_field(fields, 10))
    area_raw = clean_text(_field(fields, 11))
    area_unit = clean_text(_field(fields, 12)).upper()
    sale_date = _parse_yyyymmdd_or_blank(_field(fields, 13))
    sale_price = _parse_decimal(_field(fields, 15))
    strata_type = _map_strata_type(_field(fields, 17))
    is_multi_property_sale = parse_yes_no(_field(fields, 21))
    dealing_number = clean_text(_field(fields, 23))

    address = build_address(unit_num, house_num, street_name, suburb, postcode)
    area_sqm = convert_area_to_sqm(area_raw, area_unit)

    row = {
        "id": "",
        "url_property_id": property_id,
        "address": address,
        "sale_price": sale_price,
        "sale_date": sale_date,
        "area_sqm": area_sqm,
        "strata_type": strata_type,
        "is_multi_property_sale": is_multi_property_sale,
        "property_number": None,
        "dealing_number": dealing_number,
        "extraction_date": extraction_date,
        "unit_num": unit_num,
        "house_num": house_num,
        "street_name": street_name,
        "suburb": suburb,
        "state": "NSW",
        "postcode": postcode,
        "source_file": source_file,
        "downloaded_at": downloaded_at.isoformat(),
        "imported_at": "",
    }

    raw = {
        "district_code": district_code,
        "url_property_id": str(property_id or ""),
        "sale_record_number": sale_record_number,
        "extraction_date": extraction_date,
        "unit_num": unit_num,
        "house_num": house_num,
        "street_name": street_name,
        "suburb": suburb,
        "postcode": postcode,
        "area_raw": area_raw,
        "area_unit": area_unit,
        "sale_date": sale_date,
        "sale_price": str(sale_price or ""),
        "zoning": clean_text(_field(fields, 16)),
        "property_type_code": clean_text(_field(fields, 17)),
        "property_description": clean_text(_field(fields, 18)),
        "sale_code": clean_text(_field(fields, 20)),
        "is_multi_property_sale_raw": clean_text(_field(fields, 21)),
        "dealing_number": dealing_number,
    }
    return row, raw


def dedupe_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for row in rows:
        key = dat_duplicate_key(row)
        if key in seen:
            duplicate = dict(row)
            duplicate["skip_reason"] = "duplicate_in_source_batch"
            duplicates.append(duplicate)
        else:
            seen.add(key)
            unique.append(row)
    logger.info("weekly_dedupe_complete input_rows=%s unique_rows=%s duplicate_rows=%s", len(rows), len(unique), len(duplicates))
    return unique, duplicates


def dat_duplicate_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("url_property_id") or ""),
        str(row.get("sale_date") or ""),
        str(row.get("sale_price") or ""),
        str(row.get("dealing_number") or ""),
    )


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_source = Counter(str(row.get("source_file", "")).split("#")[-1] for row in rows)
    return {
        "rows": len(rows),
        "files": len(by_source),
        "top_files": by_source.most_common(10),
    }


def build_address(unit_num: str, house_num: str, street_name: str, suburb: str, postcode: str) -> str:
    street_bits = []
    if unit_num and house_num:
        street_bits.append(f"{unit_num}/{house_num}")
    elif unit_num:
        street_bits.append(unit_num)
    elif house_num:
        street_bits.append(house_num)
    if street_name:
        street_bits.append(street_name)
    street = " ".join(street_bits)
    locality = " ".join(part for part in [suburb, "NSW", postcode] if part)
    return ", ".join(part for part in [street, locality] if part)


def convert_area_to_sqm(area_raw: str, area_unit: str) -> Decimal | None:
    area = _parse_decimal(area_raw)
    if area is None:
        return None
    if area_unit == "H":
        return area * Decimal("10000")
    return area


def _http_get(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/zip,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urlopen(request, timeout=120) as response:
            return response.read()
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} while downloading {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Unable to connect to {url}: {exc.reason}") from exc


def _record_key(fields: list[str]) -> tuple[str, str, str]:
    return (_field(fields, 1), _field(fields, 2), _field(fields, 3))


def _field(fields: list[str], index: int) -> str:
    return fields[index].strip() if index < len(fields) else ""


def _parse_int(value: str) -> int | None:
    text = clean_text(value).replace(",", "")
    return int(text) if text else None


def _parse_decimal(value: str) -> Decimal | None:
    text = clean_text(value).replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid DAT decimal value: {value}") from exc


def _parse_yyyymmdd(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def _parse_yyyymmdd_or_blank(value: str) -> str:
    text = clean_text(value)
    if not text:
        return ""
    return _parse_yyyymmdd(text).isoformat()


def _parse_dat_datetime_date(value: str) -> str:
    text = clean_text(value)
    if not text:
        return ""
    return datetime.strptime(text[:8], "%Y%m%d").date().isoformat()


def _map_strata_type(value: str) -> str | None:
    text = clean_text(value).upper()
    if text == "S":
        return "STRATA"
    if text in {"N", "R"}:
        return "NON STRATA"
    return None


def validate_output_schema(rows: list[dict[str, Any]]) -> None:
    expected = set(OUTPUT_COLUMNS)
    for index, row in enumerate(rows, start=1):
        missing = expected.difference(row)
        extra = set(row).difference(expected)
        if missing or extra:
            raise ValueError(f"Row {index} does not match target schema. Missing={sorted(missing)} Extra={sorted(extra)}")
