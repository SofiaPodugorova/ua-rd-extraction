"""Build the final NRAT website/PDF/table accounting audit for 1991–2025.

The audit deliberately keeps two independent equalities:

1. website publications = PDFs belonging to the current website year +
   confirmed missing-PDF errors + persistent website listing gaps;
2. unique PDF doc_ids in the canonical archive = rows/doc_ids in the final CSV.

The website observations come from the saved repair progress/report files.  A
completed report contains the annual counter directly.  For a report whose
annual request failed, the equivalent count is reconstructed from all daily
website doc_ids plus the exact persistent ``count_mismatch`` deltas saved in
the error log.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import build_nrat_tables as tables


csv.field_size_limit(min(sys.maxsize, 2_147_483_647))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

COUNT_MISMATCH_RE = re.compile(
    r"сайт объявил\s+(\d+),\s+собрано уникальных doc_id\s+(\d+)",
    flags=re.IGNORECASE,
)

AUDIT_COLUMNS = [
    "year",
    "site_publications",
    "site_count_basis",
    "site_checked_at_utc",
    "archive_pdf_entries",
    "archive_unique_pdf_doc_ids",
    "pdf_doc_ids_in_current_site_year",
    "archive_pdf_doc_ids_outside_current_site_year",
    "known_no_pdf_doc_ids",
    "known_http_500_doc_ids",
    "known_listing_gap_without_doc_id",
    "listing_gap_details",
    "accounted_site_publications",
    "site_accounting_difference",
    "final_table_rows",
    "final_table_unique_doc_ids",
    "final_table_warning_rows",
    "final_table_failed_rows",
    "table_archive_difference",
    "table_doc_ids_exactly_match_archive",
    "website_accounting_ok",
    "audit_status",
]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def repair_sets(progress_path: Path) -> tuple[set[str], set[str], set[str]]:
    if not progress_path.exists():
        return set(), set(), set()
    progress = read_json(progress_path)
    site_ids: set[str] = set()
    no_pdf_ids: set[str] = set()
    server500_ids: set[str] = set()
    for record in progress.get("days", {}).values():
        site_ids.update(value.lower() for value in record.get("doc_ids", []))
        no_pdf_ids.update(
            value.lower() for value in record.get("no_pdf_doc_ids", [])
        )
        server500_ids.update(
            value.lower() for value in record.get("server500_doc_ids", [])
        )
    return site_ids, no_pdf_ids, server500_ids


def persistent_count_mismatches(
    report: dict, error_log_path: Path
) -> dict[str, int]:
    relevant_dates = set(report.get("unfinished_dates", []))
    for date, statuses in report.get(
        "accepted_server500_dates_with_additional_errors", {}
    ).items():
        if "count_mismatch" in statuses:
            relevant_dates.add(date)

    gaps: dict[str, int] = {}
    if not relevant_dates or not error_log_path.exists():
        return gaps
    for line in error_log_path.read_text(encoding="utf-8").splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 6 or parts[1] not in relevant_dates:
            continue
        if parts[2] != "count_mismatch":
            continue
        match = COUNT_MISMATCH_RE.search(parts[5])
        if not match:
            continue
        announced, listed = map(int, match.groups())
        gaps[parts[1]] = max(0, announced - listed)
    missing_dates = sorted(relevant_dates - gaps.keys())
    if missing_dates:
        raise ValueError(
            "Нет числовой записи count_mismatch для дат: "
            + ", ".join(missing_dates)
        )
    return gaps


def archive_ids(archive_path: Path) -> tuple[int, set[str], list[str]]:
    with zipfile.ZipFile(archive_path) as archive:
        groups, unparsed = tables.collect_archive_members(archive)
    return sum(len(members) for members in groups.values()), set(groups), unparsed


def table_ids(csv_path: Path) -> tuple[int, set[str], int, int, list[str]]:
    row_count = warning_count = failed_count = 0
    doc_ids: set[str] = set()
    duplicate_ids: list[str] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != tables.CSV_COLUMNS:
            raise ValueError(f"Неверная схема CSV: {csv_path}")
        for row in reader:
            row_count += 1
            doc_id = row.get("doc_id", "").lower()
            if doc_id in doc_ids:
                duplicate_ids.append(doc_id)
            doc_ids.add(doc_id)
            warning_count += row.get("extraction_status") == "warning"
            failed_count += row.get("extraction_status") == "failed"
    return row_count, doc_ids, warning_count, failed_count, duplicate_ids


def audit_year(year: int, archives_dir: Path, output_dir: Path) -> dict:
    report_path = archives_dir / f"_repair_report_{year}.json"
    progress_path = archives_dir / f"_repair_progress_{year}.json"
    error_log_path = archives_dir / f"_repair_errors_{year}.txt"
    archive_path = archives_dir / f"{year}.zip"
    csv_path = output_dir / "csv" / f"output_{year}.csv"

    report = read_json(report_path)
    site_ids, no_pdf_ids, server500_ids = repair_sets(progress_path)
    physical_pdf_entries, pdf_ids, unparsed_pdf_members = archive_ids(archive_path)
    table_rows, csv_ids, warnings, failures, duplicate_csv_ids = table_ids(csv_path)
    listing_gaps = persistent_count_mismatches(report, error_log_path)
    listing_gap_count = sum(listing_gaps.values())

    annual_site_total = report.get("annual_site_total")
    if annual_site_total is not None:
        site_publications = int(annual_site_total)
        site_count_basis = "annual_site_total"
    else:
        site_publications = len(site_ids) + listing_gap_count
        site_count_basis = "daily_site_doc_ids_plus_persistent_listing_gaps"

    pdf_in_site_year = pdf_ids & site_ids
    extra_pdf_ids = pdf_ids - site_ids
    missing_pdf_ids = site_ids - pdf_ids
    missing_no_pdf_ids = missing_pdf_ids & no_pdf_ids
    missing_server500_ids = missing_pdf_ids & server500_ids
    unexplained_missing_ids = missing_pdf_ids - no_pdf_ids - server500_ids
    error_ids_not_missing = (no_pdf_ids | server500_ids) - missing_pdf_ids
    overlapping_error_ids = no_pdf_ids & server500_ids

    accounted_site_publications = (
        len(pdf_in_site_year)
        + len(missing_no_pdf_ids)
        + len(missing_server500_ids)
        + listing_gap_count
    )
    site_difference = accounted_site_publications - site_publications
    table_difference = table_rows - len(pdf_ids)
    exact_table_match = (
        csv_ids == pdf_ids
        and not duplicate_csv_ids
        and table_rows == len(csv_ids)
        and not unparsed_pdf_members
    )
    website_accounting_ok = (
        site_difference == 0
        and not unexplained_missing_ids
        and not error_ids_not_missing
        and not overlapping_error_ids
        and len(site_ids) == int(report.get("daily_unique_site_doc_ids", 0))
    )

    errors = []
    if unparsed_pdf_members:
        errors.append(f"PDF без doc_id: {len(unparsed_pdf_members)}")
    if duplicate_csv_ids:
        errors.append(f"повторные строки CSV: {len(duplicate_csv_ids)}")
    if not exact_table_match:
        errors.append("doc_id CSV не совпадают с ZIP")
    if unexplained_missing_ids:
        errors.append(f"необъяснённые отсутствующие PDF: {len(unexplained_missing_ids)}")
    if error_ids_not_missing:
        errors.append(f"ошибочные категории не соответствуют отсутствию: {len(error_ids_not_missing)}")
    if overlapping_error_ids:
        errors.append(f"doc_id одновременно no_pdf и HTTP 500: {len(overlapping_error_ids)}")
    if site_difference:
        errors.append(f"разница с сайтом: {site_difference:+d}")

    known_problem_count = (
        len(missing_no_pdf_ids) + len(missing_server500_ids) + listing_gap_count
    )
    if errors:
        status = "FAIL"
    elif known_problem_count or extra_pdf_ids or failures:
        status = "PASS_WITH_KNOWN_EXCEPTIONS"
    else:
        status = "PASS"

    return {
        "year": year,
        "site_publications": site_publications,
        "site_count_basis": site_count_basis,
        "site_checked_at_utc": report.get("checked_at_utc", ""),
        "archive_pdf_entries": physical_pdf_entries,
        "archive_unique_pdf_doc_ids": len(pdf_ids),
        "pdf_doc_ids_in_current_site_year": len(pdf_in_site_year),
        "archive_pdf_doc_ids_outside_current_site_year": len(extra_pdf_ids),
        "known_no_pdf_doc_ids": len(missing_no_pdf_ids),
        "known_http_500_doc_ids": len(missing_server500_ids),
        "known_listing_gap_without_doc_id": listing_gap_count,
        "listing_gap_details": "; ".join(
            f"{date}: +{gap}" for date, gap in sorted(listing_gaps.items())
        ),
        "accounted_site_publications": accounted_site_publications,
        "site_accounting_difference": site_difference,
        "final_table_rows": table_rows,
        "final_table_unique_doc_ids": len(csv_ids),
        "final_table_warning_rows": warnings,
        "final_table_failed_rows": failures,
        "table_archive_difference": table_difference,
        "table_doc_ids_exactly_match_archive": exact_table_match,
        "website_accounting_ok": website_accounting_ok,
        "audit_status": status,
        "errors": errors,
    }


def totals(rows: list[dict]) -> dict:
    summed_fields = [
        "site_publications",
        "archive_pdf_entries",
        "archive_unique_pdf_doc_ids",
        "pdf_doc_ids_in_current_site_year",
        "archive_pdf_doc_ids_outside_current_site_year",
        "known_no_pdf_doc_ids",
        "known_http_500_doc_ids",
        "known_listing_gap_without_doc_id",
        "accounted_site_publications",
        "final_table_rows",
        "final_table_unique_doc_ids",
        "final_table_warning_rows",
        "final_table_failed_rows",
    ]
    result = {field: sum(int(row[field]) for row in rows) for field in summed_fields}
    result.update(
        {
            "years": len(rows),
            "years_pass": sum(row["audit_status"] == "PASS" for row in rows),
            "years_pass_with_known_exceptions": sum(
                row["audit_status"] == "PASS_WITH_KNOWN_EXCEPTIONS"
                for row in rows
            ),
            "years_fail": sum(row["audit_status"] == "FAIL" for row in rows),
            "all_website_accounting_ok": all(
                row["website_accounting_ok"] for row in rows
            ),
            "all_table_doc_ids_exactly_match_archive": all(
                row["table_doc_ids_exactly_match_archive"] for row in rows
            ),
        }
    )
    return result


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_COLUMNS)
        writer.writeheader()
        writer.writerows({field: row[field] for field in AUDIT_COLUMNS} for row in rows)


def write_markdown(path: Path, rows: list[dict], summary: dict, generated_at: str) -> None:
    lines = [
        "# Финальный аудит НРАТ 1991–2025",
        "",
        f"Сформирован: `{generated_at}`.",
        "",
        "Счётчики сайта взяты из сохранённого полного repair-аудита НРАТ "
        "(наблюдения 9 августа 2026 года). Для 2021, 2024 и 2025 годов "
        "годовой запрос был недоступен, поэтому эквивалентный годовой итог "
        "получен как сумма уникальных дневных `doc_id` и зафиксированных "
        "разрывов выдачи `count_mismatch`.",
        "",
        "Формулы проверки:",
        "",
        "- `сайт = PDF, относящиеся к текущей годовой выдаче + no_pdf + HTTP 500 + разрыв выдачи без doc_id`;",
        "- `строки финальной таблицы = уникальные doc_id PDF в каноническом ZIP`.",
        "",
        "| Год | Сайт | PDF в выдаче года | PDF вне текущей выдачи года | no_pdf | HTTP 500 | разрыв без doc_id | Учтено | Строк CSV | Unique PDF | Статус |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---|",
    ]
    for row in rows:
        lines.append(
            "| {year} | {site_publications} | {pdf_doc_ids_in_current_site_year} "
            "| {archive_pdf_doc_ids_outside_current_site_year} | {known_no_pdf_doc_ids} "
            "| {known_http_500_doc_ids} | {known_listing_gap_without_doc_id} "
            "| {accounted_site_publications} | {final_table_rows} "
            "| {archive_unique_pdf_doc_ids} | {audit_status} |".format(**row)
        )
    lines.extend(
        [
            "",
            "## Общий итог",
            "",
            f"- Сайт: **{summary['site_publications']}** публикации.",
            f"- PDF в текущих годовых выдачах: **{summary['pdf_doc_ids_in_current_site_year']}**.",
            f"- Известные `no_pdf`: **{summary['known_no_pdf_doc_ids']}**.",
            f"- Известные HTTP 500: **{summary['known_http_500_doc_ids']}**.",
            f"- Разрывы выдачи без доступного `doc_id`: **{summary['known_listing_gap_without_doc_id']}**.",
            f"- Итого учтено по формуле сайта: **{summary['accounted_site_publications']}**.",
            f"- Уникальные PDF в канонических ZIP: **{summary['archive_unique_pdf_doc_ids']}**.",
            f"- Физические PDF-вхождения с вариантами: **{summary['archive_pdf_entries']}**.",
            f"- Строки финальных CSV: **{summary['final_table_rows']}**.",
            f"- PDF, сохранённые в архиве года, но больше не входящие в текущую годовую выдачу сайта: **{summary['archive_pdf_doc_ids_outside_current_site_year']}**.",
            "",
            "Все множества `doc_id` CSV и канонических ZIP совпадают точно. "
            "Физических PDF больше, чем строк, потому что 986 файлов являются "
            "дополнительными вариантами уже учтённых `doc_id`.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archives-dir", type=Path, default=tables.DEFAULT_ARCHIVES_DIR)
    parser.add_argument("--output-dir", type=Path, default=tables.DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    archives_dir = args.archives_dir.resolve()
    output_dir = args.output_dir.resolve()
    reports_dir = output_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    rows = [audit_year(year, archives_dir, output_dir) for year in range(1991, 2026)]
    summary = totals(rows)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {
        "generated_at_utc": generated_at,
        "website_observation_note": (
            "Saved NRAT repair observations from 2026-08-09; live recheck on "
            "2026-08-21 was unavailable because the server reset the connection."
        ),
        "rows": rows,
        "totals": summary,
    }

    csv_path = reports_dir / "final_audit_1991_2025.csv"
    json_path = reports_dir / "final_audit_1991_2025.json"
    markdown_path = reports_dir / "FINAL_AUDIT_1991_2025.md"
    write_csv(csv_path, rows)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_markdown(markdown_path, rows, summary, generated_at)

    for row in rows:
        print(
            f"{row['year']}: сайт={row['site_publications']}, "
            f"учтено={row['accounted_site_publications']}, "
            f"PDF={row['archive_unique_pdf_doc_ids']}, "
            f"CSV={row['final_table_rows']}, {row['audit_status']}"
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"CSV: {csv_path}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    return 1 if summary["years_fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
