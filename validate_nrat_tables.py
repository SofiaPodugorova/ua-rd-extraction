"""Validate rebuilt NRAT CSV files against canonical annual ZIP indexes."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import build_nrat_tables as tables


csv.field_size_limit(min(sys.maxsize, 2_147_483_647))


def validate_year(year: int, archives_dir: Path, output_dir: Path) -> dict:
    archive_path = archives_dir / f"{year}.zip"
    csv_path = output_dir / "csv" / f"output_{year}.csv"
    errors: list[str] = []

    with zipfile.ZipFile(archive_path) as archive:
        groups, unparsed = tables.collect_archive_members(archive)
        archive_members = {
            member for members in groups.values() for member in members
        }

    if unparsed:
        errors.append(f"archive PDF without doc_id: {len(unparsed)}")

    raw_prefix = csv_path.read_bytes()[:3]
    if raw_prefix != b"\xef\xbb\xbf":
        errors.append("CSV has no UTF-8 BOM")

    csv_doc_ids: list[str] = []
    warning = failed = 0
    max_cell_chars = 0
    cells_over_excel_limit = 0
    missing_selected_members = 0
    bad_variant_lists = 0
    raw_co_performer_blocks = 0
    malformed_research_leaders_json = 0
    organization_label_leaks = 0
    nonempty_orcid = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != tables.CSV_COLUMNS:
            errors.append(
                f"CSV header differs from canonical {len(tables.CSV_COLUMNS)}-column schema"
            )
        for row in reader:
            csv_doc_ids.append(row.get("doc_id", ""))
            warning += row.get("extraction_status") == "warning"
            failed += row.get("extraction_status") == "failed"
            if row.get("source_member") not in archive_members:
                missing_selected_members += 1
            try:
                variants = json.loads(row.get("source_variants", "[]"))
                expected = groups.get(row.get("doc_id", ""), [])
                if sorted(variants) != sorted(expected):
                    bad_variant_lists += 1
            except (json.JSONDecodeError, TypeError):
                bad_variant_lists += 1
            for field in ("co_performers", "research_leaders"):
                value = row.get(field, "")
                if not value:
                    continue
                try:
                    parsed = json.loads(value)
                    if not isinstance(parsed, list):
                        if field == "co_performers":
                            raw_co_performer_blocks += 1
                        else:
                            malformed_research_leaders_json += 1
                except (json.JSONDecodeError, TypeError):
                    if field == "co_performers":
                        raw_co_performer_blocks += 1
                    else:
                        malformed_research_leaders_json += 1
            organization_label_leaks += any(
                "(або ПІБ фізичної особи)" in row.get(field, "")
                for field in ("performer_name", "customer_name")
            )
            nonempty_orcid += bool(row.get("pi_orcid"))
            for value in row.values():
                length = len(value or "")
                max_cell_chars = max(max_cell_chars, length)
                cells_over_excel_limit += length > 32_767

    csv_set = set(csv_doc_ids)
    archive_set = set(groups)
    missing_doc_ids = sorted(archive_set - csv_set)
    extra_doc_ids = sorted(csv_set - archive_set)
    duplicate_rows = len(csv_doc_ids) - len(csv_set)
    if missing_doc_ids:
        errors.append(f"missing doc_id: {len(missing_doc_ids)}")
    if extra_doc_ids:
        errors.append(f"extra doc_id: {len(extra_doc_ids)}")
    if duplicate_rows:
        errors.append(f"duplicate CSV rows: {duplicate_rows}")
    if missing_selected_members:
        errors.append(f"selected source member absent from ZIP: {missing_selected_members}")
    if bad_variant_lists:
        errors.append(f"source_variants mismatch: {bad_variant_lists}")
    if malformed_research_leaders_json:
        errors.append(
            "malformed research_leaders JSON cells: "
            f"{malformed_research_leaders_json}"
        )
    if organization_label_leaks:
        errors.append(f"organization label leaked into values: {organization_label_leaks}")

    return {
        "year": year,
        "archive_pdf_entries": len(archive_members),
        "archive_unique_doc_ids": len(archive_set),
        "csv_rows": len(csv_doc_ids),
        "csv_unique_doc_ids": len(csv_set),
        "warning_rows": warning,
        "failed_rows": failed,
        "max_cell_chars": max_cell_chars,
        "cells_over_excel_limit": cells_over_excel_limit,
        "nonempty_orcid": nonempty_orcid,
        "raw_co_performer_blocks": raw_co_performer_blocks,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archives-dir", type=Path, default=tables.DEFAULT_ARCHIVES_DIR)
    parser.add_argument("--output-dir", type=Path, default=tables.DEFAULT_OUTPUT_DIR)
    parser.add_argument("--start-year", type=int, default=1991)
    parser.add_argument("--end-year", type=int, default=2025)
    args = parser.parse_args()

    results = []
    for year in range(args.start_year, args.end_year + 1):
        result = validate_year(year, args.archives_dir.resolve(), args.output_dir.resolve())
        results.append(result)
        status = "OK" if not result["errors"] else "ERROR"
        print(
            f"{year}: {status}, rows={result['csv_rows']}, "
            f"doc_id={result['archive_unique_doc_ids']}, "
            f"warning={result['warning_rows']}, failed={result['failed_rows']}"
        )

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "years": results,
        "totals": {
            "archive_pdf_entries": sum(row["archive_pdf_entries"] for row in results),
            "archive_unique_doc_ids": sum(
                row["archive_unique_doc_ids"] for row in results
            ),
            "csv_rows": sum(row["csv_rows"] for row in results),
            "warning_rows": sum(row["warning_rows"] for row in results),
            "failed_rows": sum(row["failed_rows"] for row in results),
            "cells_over_excel_limit": sum(
                row["cells_over_excel_limit"] for row in results
            ),
            "years_with_errors": sum(bool(row["errors"]) for row in results),
        },
    }
    report_path = args.output_dir.resolve() / "reports" / "validation_1991_2025.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["totals"], ensure_ascii=False, indent=2))
    print(f"Validation: {report_path}")
    return 1 if report["totals"]["years_with_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
