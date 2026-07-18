from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from six_nsw_property_download.transform import OUTPUT_COLUMNS
from six_nsw_property_download.weekly_dat import WeeklyZip, dedupe_rows, parse_weekly_zip, validate_output_schema
from six_nsw_property_download.weekly_pipeline import split_uploadable_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Export source-deduped weekly DAT rows without any database access.")
    parser.add_argument("--week", action="append", type=parse_iso_date, required=True)
    parser.add_argument("--work-dir", default="data\\valuation_weekly")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    zip_dir = Path(args.work_dir) / "zip"

    all_rows: list[dict[str, object]] = []
    summary_rows: list[dict[str, object]] = []

    for week in args.week:
        zip_path = zip_dir / f"{week.strftime('%Y%m%d')}.zip"
        content = zip_path.read_bytes()
        weekly_zip = WeeklyZip(
            week=week,
            url=f"https://www.valuergeneral.nsw.gov.au/__psi/weekly/{week.strftime('%Y%m%d')}.zip",
            content=content,
        )
        parsed = parse_weekly_zip(weekly_zip)
        validate_output_schema(parsed.rows)
        unique_rows, duplicate_rows = dedupe_rows(parsed.rows)
        uploadable_rows, invalid_rows = split_uploadable_rows(unique_rows)

        for row in uploadable_rows:
            row["dry_run_week"] = week.isoformat()
            row["dedupe_scope"] = "source_batch_only"

        all_rows.extend(uploadable_rows)
        summary_rows.append(
            {
                "week": week.isoformat(),
                "parsed_rows": len(parsed.rows),
                "source_duplicate_rows": len(duplicate_rows),
                "invalid_rows": len(invalid_rows),
                "exported_rows": len(uploadable_rows),
                "database_duplicate_check": "not_run",
            }
        )

    columns = ["dry_run_week", "dedupe_scope"] + [column for column in OUTPUT_COLUMNS if column != "id"]
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    summary_path = output_path.with_suffix(".summary.csv")
    with summary_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(summary_rows[0].keys()) if summary_rows else [])
        writer.writeheader()
        writer.writerows(summary_rows)

    print(f"records_csv={output_path}")
    print(f"summary_csv={summary_path}")
    print(f"records={len(all_rows)}")


def parse_iso_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use YYYY-MM-DD, for example 2026-06-29.") from exc


if __name__ == "__main__":
    main()
