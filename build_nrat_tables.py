"""Build complete per-year NRAT CSV datasets from canonical PDF ZIP files.

The canonical input is ``../nrat pdfs/<year>.zip``.  Each output row represents
one unique NRAT ``doc_id``.  If an archive contains several PDF variants for
the same ``doc_id``, every variant is parsed and the most complete record is
selected; all member names remain recorded in ``source_variants``.

CSV is the auditable intermediate format used to create the final XLSX files.
It is written with an UTF-8 BOM so it also opens correctly in Excel directly.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

import fitz

import extract_rd_data as extractor


csv.field_size_limit(min(sys.maxsize, 2_147_483_647))


REPO_DIR = Path(__file__).resolve().parent
DEFAULT_ARCHIVES_DIR = REPO_DIR.parent / "nrat pdfs"
DEFAULT_OUTPUT_DIR = REPO_DIR.parent / "outputs" / "nrat_excel_1991_2025"
DEFAULT_START_YEAR = 1991
DEFAULT_END_YEAR = 2025

DOC_ID_RE = re.compile(r"([0-9a-f]{32})", re.IGNORECASE)
REGISTRATION_RE = re.compile(r"(\d{4}[A-Za-zА-Яа-яІіЇїЄє]\d+)")

SYSTEM_COLUMNS = [
    "doc_id",
    "source_file",
    "source_member",
    "source_variants",
    "pdf_variant_count",
    "year",
    "language",
    "extraction_status",
    "extraction_error",
    "quality_warnings",
    "missing_critical_fields",
    "extracted_text_chars",
    "section_count",
    "registration_number_filename",
    "registration_number_matches_filename",
]

CSV_COLUMNS = (
    SYSTEM_COLUMNS
    + extractor.DATA_COLUMNS
    + [f"lang_{field}" for field in extractor.TEXT_FIELDS]
)

QUALITY_ISSUE_COLUMNS = [
    "year",
    "doc_id",
    "registration_number",
    "source_member",
    "extraction_status",
    "quality_warnings",
    "missing_critical_fields",
    "extraction_error",
]


@dataclass(frozen=True)
class ParsedVariant:
    member: str
    record: dict[str, object]
    quality: tuple[int, int, int, int, int, int]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"nrat_table_builder.{log_path.stem}")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


def doc_id_from_name(name: str) -> str | None:
    """Return the last 32-hex token from a PDF basename."""
    matches = DOC_ID_RE.findall(PurePosixPath(name).name)
    return matches[-1].lower() if matches else None


def registration_from_name(name: str) -> str:
    match = REGISTRATION_RE.search(PurePosixPath(name).name)
    return match.group(1) if match else ""


def collect_archive_members(
    archive: zipfile.ZipFile,
) -> tuple[dict[str, list[str]], list[str]]:
    """Group PDF members by doc_id and return unparseable member names."""
    grouped: dict[str, list[str]] = defaultdict(list)
    unparsed: list[str] = []
    for info in archive.infolist():
        if info.is_dir() or not info.filename.lower().endswith(".pdf"):
            continue
        doc_id = doc_id_from_name(info.filename)
        if doc_id:
            grouped[doc_id].append(info.filename)
        else:
            unparsed.append(info.filename)
    for members in grouped.values():
        members.sort()
    return dict(grouped), sorted(unparsed)


def pdf_bytes_to_text(data: bytes) -> str:
    """Extract stable Unicode text from an in-memory PDF."""
    document = fitz.open(stream=data, filetype="pdf")
    try:
        return "\n".join(extractor.page_to_text(page) for page in document)
    finally:
        document.close()


def parse_pdf_variant(
    data: bytes,
    *,
    member: str,
    doc_id: str,
    year: int,
) -> ParsedVariant:
    """Parse one PDF variant and attach explicit extraction-quality fields."""
    full_text = pdf_bytes_to_text(data)
    sections = extractor.split_sections(full_text)

    record: dict[str, object] = {}
    record.update(extractor.parse_section_i(sections["I"]))
    record.update(extractor.parse_section_ii(sections["II"]))
    record.update(extractor.parse_section_iii(sections["III"]))
    record.update(extractor.parse_section_iv(sections["IV"]))
    record.update(extractor.parse_section_v(sections["V"]))
    record.update(extractor.parse_section_vi(sections["VI"]))
    record.update(extractor.parse_section_vii(sections["VII"]))
    record.update(extractor.parse_section_viii(sections["VIII"]))
    record.update(extractor.parse_section_ix(sections["IX"]))
    record.update(extractor.parse_section_x(sections["X"]))

    for field in extractor.TEXT_FIELDS:
        value = str(record.get(field, "") or "")
        record[f"lang_{field}"] = extractor.detect_field_language(value)

    source_file = PurePosixPath(member).name
    filename_registration = registration_from_name(source_file)
    parsed_registration = str(record.get("registration_number", "") or "")
    missing_critical = [
        field for field in extractor.CRITICAL_FIELDS if not record.get(field)
    ]
    section_count = sum(bool(value.strip()) for value in sections.values())
    nonempty_data = sum(
        bool(str(record.get(field, "") or "").strip())
        for field in extractor.DATA_COLUMNS
    )

    warnings: list[str] = []
    if missing_critical:
        warnings.append("missing_critical_fields")
    if section_count < 6:
        warnings.append("few_nonempty_sections")
    if filename_registration and parsed_registration:
        registration_matches = parsed_registration == filename_registration
        if not registration_matches:
            warnings.append("registration_number_mismatch")
    else:
        registration_matches = None

    record.update(
        {
            "doc_id": doc_id,
            "source_file": source_file,
            "source_member": member,
            "year": year,
            "language": extractor.detect_row_language(record),
            "extraction_status": "warning" if warnings else "ok",
            "extraction_error": "",
            "quality_warnings": ";".join(warnings),
            "missing_critical_fields": ";".join(missing_critical),
            "extracted_text_chars": len(full_text),
            "section_count": section_count,
            "registration_number_filename": filename_registration,
            "registration_number_matches_filename": (
                "yes" if registration_matches is True
                else "no" if registration_matches is False
                else "unknown"
            ),
        }
    )

    # Prefer a readable record, then more critical fields, more populated
    # sections/columns, and finally more extracted source text.
    quality = (
        1,
        1 if registration_matches is True else 0,
        len(extractor.CRITICAL_FIELDS) - len(missing_critical),
        section_count,
        nonempty_data,
        len(full_text),
    )
    return ParsedVariant(member=member, record=record, quality=quality)


def failed_record(
    *,
    doc_id: str,
    year: int,
    members: list[str],
    errors: list[str],
) -> dict[str, object]:
    selected = members[0] if members else ""
    source_file = PurePosixPath(selected).name if selected else ""
    return {
        "doc_id": doc_id,
        "source_file": source_file,
        "source_member": selected,
        "source_variants": json.dumps(members, ensure_ascii=False),
        "pdf_variant_count": len(members),
        "year": year,
        "language": "unknown",
        "extraction_status": "failed",
        "extraction_error": " | ".join(errors),
        "quality_warnings": "pdf_parse_failed",
        "missing_critical_fields": ";".join(extractor.CRITICAL_FIELDS),
        "extracted_text_chars": 0,
        "section_count": 0,
        "registration_number_filename": registration_from_name(source_file),
        "registration_number_matches_filename": "unknown",
    }


def choose_best_variant(
    archive: zipfile.ZipFile,
    *,
    doc_id: str,
    members: list[str],
    year: int,
    logger: logging.Logger,
) -> dict[str, object]:
    parsed: list[ParsedVariant] = []
    errors: list[str] = []
    for member in members:
        try:
            parsed.append(
                parse_pdf_variant(
                    archive.read(member), member=member, doc_id=doc_id, year=year
                )
            )
        except Exception as exc:  # one bad variant must not remove the doc_id
            message = f"{member}: {type(exc).__name__}: {exc}"
            errors.append(message)
            logger.error("FAIL %s", message)

    if not parsed:
        return failed_record(doc_id=doc_id, year=year, members=members, errors=errors)

    best = max(parsed, key=lambda item: (item.quality, item.member))
    record = dict(best.record)
    record["source_variants"] = json.dumps(members, ensure_ascii=False)
    record["pdf_variant_count"] = len(members)
    if errors:
        record["extraction_error"] = " | ".join(errors)
        warnings = [item for item in str(record["quality_warnings"]).split(";") if item]
        warnings.append("other_variant_parse_failed")
        record["quality_warnings"] = ";".join(dict.fromkeys(warnings))
        record["extraction_status"] = "warning"
    return record


def write_row(writer: csv.DictWriter, record: dict[str, object]) -> None:
    writer.writerow({column: record.get(column, "") for column in CSV_COLUMNS})


def build_year(
    *,
    year: int,
    archive_path: Path,
    csv_path: Path,
    report_path: Path,
    log_path: Path,
    sample: int = 0,
) -> dict[str, object]:
    """Build one atomic CSV and its quality report."""
    logger = setup_logging(log_path)
    started = time.perf_counter()
    logger.info("YEAR %d: %s", year, archive_path)

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temp_csv = csv_path.with_suffix(csv_path.suffix + ".tmp")
    if temp_csv.exists():
        temp_csv.unlink()

    with zipfile.ZipFile(archive_path) as archive:
        # Every selected member is read through ZipFile.read(), which validates
        # its CRC.  A separate testzip() would read every multi-gigabyte archive
        # twice during a full build.
        grouped, unparsed_members = collect_archive_members(archive)
        if unparsed_members:
            examples = ", ".join(unparsed_members[:5])
            raise RuntimeError(
                f"{archive_path.name}: {len(unparsed_members)} PDF without doc_id: {examples}"
            )

        all_doc_ids = sorted(grouped)
        selected_doc_ids = all_doc_ids
        if sample:
            step = max(1, len(all_doc_ids) // sample)
            selected_doc_ids = all_doc_ids[::step][:sample]
            logger.info("SAMPLE: %d of %d doc_id", len(selected_doc_ids), len(all_doc_ids))

        counters = {"ok": 0, "warning": 0, "failed": 0}
        mismatch_count = 0
        with temp_csv.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for index, doc_id in enumerate(selected_doc_ids, 1):
                members = grouped[doc_id]
                record = choose_best_variant(
                    archive,
                    doc_id=doc_id,
                    members=members,
                    year=year,
                    logger=logger,
                )
                status = str(record["extraction_status"])
                counters[status] += 1
                mismatch_count += record["registration_number_matches_filename"] == "no"
                write_row(writer, record)
                if index % 250 == 0 or index == len(selected_doc_ids):
                    elapsed = max(time.perf_counter() - started, 0.001)
                    logger.info(
                        "Progress: %d/%d (%.1f doc_id/sec)",
                        index,
                        len(selected_doc_ids),
                        index / elapsed,
                    )

    os.replace(temp_csv, csv_path)
    report: dict[str, object] = {
        "schema_version": 2,
        "generated_at_utc": utc_now(),
        "year": year,
        "archive": str(archive_path.resolve()),
        "csv": str(csv_path.resolve()),
        "csv_columns": len(CSV_COLUMNS),
        "archive_pdf_entries": sum(len(members) for members in grouped.values()),
        "archive_unique_doc_ids": len(grouped),
        "archive_extra_variants": sum(len(members) - 1 for members in grouped.values()),
        "rows_written": len(selected_doc_ids),
        "sample_mode": bool(sample),
        "status_counts": counters,
        "registration_number_mismatches": mismatch_count,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    logger.info(
        "DONE %d: rows=%d columns=%d OK=%d WARN=%d FAIL=%d -> %s",
        year,
        len(selected_doc_ids),
        len(CSV_COLUMNS),
        counters["ok"],
        counters["warning"],
        counters["failed"],
        csv_path,
    )
    return report


def existing_output_is_complete(
    *,
    archive_path: Path,
    csv_path: Path,
    report_path: Path,
) -> bool:
    if not csv_path.is_file() or not report_path.is_file():
        return False
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        return (
            not report.get("sample_mode")
            and int(report["rows_written"]) == int(report["archive_unique_doc_ids"])
            and int(report["csv_columns"]) == len(CSV_COLUMNS)
            and Path(str(report["archive"])).resolve() == archive_path.resolve()
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def summarize_reports(reports: Iterable[dict[str, object]]) -> dict[str, object]:
    rows = list(reports)
    return {
        "schema_version": 2,
        "generated_at_utc": utc_now(),
        "years": rows,
        "totals": {
            "years": len(rows),
            "archive_pdf_entries": sum(int(row["archive_pdf_entries"]) for row in rows),
            "archive_unique_doc_ids": sum(
                int(row["archive_unique_doc_ids"]) for row in rows
            ),
            "archive_extra_variants": sum(
                int(row["archive_extra_variants"]) for row in rows
            ),
            "rows_written": sum(int(row["rows_written"]) for row in rows),
            "ok": sum(int(row["status_counts"]["ok"]) for row in rows),
            "warning": sum(
                int(row["status_counts"]["warning"]) for row in rows
            ),
            "failed": sum(int(row["status_counts"]["failed"]) for row in rows),
            "registration_number_mismatches": sum(
                int(row["registration_number_mismatches"]) for row in rows
            ),
        },
    }


def write_manifest_csv(summary: dict[str, object], path: Path) -> None:
    columns = [
        "year",
        "archive_pdf_entries",
        "archive_unique_doc_ids",
        "archive_extra_variants",
        "rows_written",
        "columns",
        "ok",
        "warning",
        "failed",
        "registration_number_mismatches",
        "csv",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        for report in summary["years"]:
            writer.writerow(
                {
                    "year": report["year"],
                    "archive_pdf_entries": report["archive_pdf_entries"],
                    "archive_unique_doc_ids": report["archive_unique_doc_ids"],
                    "archive_extra_variants": report["archive_extra_variants"],
                    "rows_written": report["rows_written"],
                    "columns": report["csv_columns"],
                    "ok": report["status_counts"]["ok"],
                    "warning": report["status_counts"]["warning"],
                    "failed": report["status_counts"]["failed"],
                    "registration_number_mismatches": report[
                        "registration_number_mismatches"
                    ],
                    "csv": report["csv"],
                }
            )


def write_quality_issues_csv(
    *,
    reports: Iterable[dict[str, object]],
    path: Path,
) -> int:
    issue_count = 0
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=QUALITY_ISSUE_COLUMNS)
        writer.writeheader()
        for report in reports:
            csv_path = Path(str(report["csv"]))
            with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
                for record in csv.DictReader(source):
                    if record["extraction_status"] == "ok":
                        continue
                    writer.writerow(
                        {column: record.get(column, "") for column in QUALITY_ISSUE_COLUMNS}
                    )
                    issue_count += 1
    return issue_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build complete per-year NRAT CSV data from canonical ZIP archives."
    )
    parser.add_argument("--archives-dir", type=Path, default=DEFAULT_ARCHIVES_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR)
    parser.add_argument("--end-year", type=int, default=DEFAULT_END_YEAR)
    parser.add_argument(
        "--sample", type=int, default=0, help="Process N evenly spaced doc_id per year."
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="Rebuild complete existing yearly CSV files."
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.start_year > args.end_year:
        raise SystemExit("--start-year must be <= --end-year")
    if args.start_year < DEFAULT_START_YEAR or args.end_year > DEFAULT_END_YEAR:
        raise SystemExit("Supported target range is 1991..2025")
    if args.sample < 0:
        raise SystemExit("--sample must be >= 0")

    output_dir = args.output_dir.resolve()
    csv_dir = output_dir / "csv"
    reports_dir = output_dir / "reports"
    logs_dir = output_dir / "logs"
    csv_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    reports: list[dict[str, object]] = []
    for year in range(args.start_year, args.end_year + 1):
        archive_path = args.archives_dir.resolve() / f"{year}.zip"
        if not archive_path.is_file():
            raise SystemExit(f"Missing canonical archive: {archive_path}")
        suffix = "_sample" if args.sample else ""
        csv_path = csv_dir / f"output_{year}{suffix}.csv"
        report_path = reports_dir / f"output_{year}{suffix}.json"
        log_path = logs_dir / f"output_{year}{suffix}.log"

        if (
            not args.overwrite
            and not args.sample
            and existing_output_is_complete(
                archive_path=archive_path,
                csv_path=csv_path,
                report_path=report_path,
            )
        ):
            print(f"SKIP {year}: complete output already exists")
            reports.append(json.loads(report_path.read_text(encoding="utf-8")))
            continue

        reports.append(
            build_year(
                year=year,
                archive_path=archive_path,
                csv_path=csv_path,
                report_path=report_path,
                log_path=log_path,
                sample=args.sample,
            )
        )

    summary = summarize_reports(reports)
    summary_name = "summary_sample.json" if args.sample else "summary_1991_2025.json"
    summary_path = reports_dir / summary_name
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not args.sample:
        manifest_path = reports_dir / "manifest_1991_2025.csv"
        issues_path = reports_dir / "quality_issues_1991_2025.csv"
        schema_path = reports_dir / f"schema_{len(CSV_COLUMNS)}_columns.json"
        write_manifest_csv(summary, manifest_path)
        issue_count = write_quality_issues_csv(reports=reports, path=issues_path)
        schema_path.write_text(
            json.dumps(
                {
                    "column_count": len(CSV_COLUMNS),
                    "columns": CSV_COLUMNS,
                    "system_columns": SYSTEM_COLUMNS,
                    "data_columns": extractor.DATA_COLUMNS,
                    "language_columns": [
                        f"lang_{field}" for field in extractor.TEXT_FIELDS
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Manifest: {manifest_path}")
        print(f"Quality issues: {issues_path} ({issue_count} rows)")
        print(f"Schema: {schema_path}")
    print(json.dumps(summary["totals"], ensure_ascii=False, indent=2))
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
