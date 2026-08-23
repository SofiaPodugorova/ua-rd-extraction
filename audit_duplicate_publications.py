"""Audit every byte-distinct NRAT PDF variant and select one publication copy.

The canonical yearly ZIP archives sometimes contain two PDF members with the
same NRAT ``doc_id``.  A different SHA-256 is not enough to call them different
publications: the NRAT service often regenerated the same PDF with another
creation timestamp or filename.  This audit therefore opens every page and
compares parsed identifiers, normalized text, and rendered page pixels.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import unicodedata
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import fitz

import build_nrat_tables as tables


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


REPO_DIR = Path(__file__).resolve().parent
DEFAULT_ARCHIVES_DIR = REPO_DIR.parent / "nrat pdfs"
DEFAULT_OUTPUT_DIR = (
    REPO_DIR.parent / "outputs" / "nrat_excel_1991_2025" / "reports"
)
DEFAULT_TABLES_DIR = REPO_DIR.parent / "outputs" / "nrat_excel_1991_2025" / "csv"

AUDIT_COLUMNS = [
    "year",
    "doc_id",
    "classification",
    "same_publication",
    "decision_basis",
    "variant_count",
    "readable_count",
    "all_pages_opened",
    "selected_member",
    "selected_source_file",
    "selected_registration_number",
    "selected_state_registration_number",
    "selected_title_uk",
    "table_source_member",
    "table_selection_matches_audit",
    "dataset_action",
    "member_a",
    "member_a_sha256",
    "member_a_bytes",
    "member_a_readable",
    "member_a_pages",
    "member_a_opened_pages",
    "member_a_registration_filename",
    "member_a_registration_internal",
    "member_a_state_registration",
    "member_a_title_uk",
    "member_a_text_chars",
    "member_a_text_sha256",
    "member_a_render_sha256",
    "member_a_error",
    "member_b",
    "member_b_sha256",
    "member_b_bytes",
    "member_b_readable",
    "member_b_pages",
    "member_b_opened_pages",
    "member_b_registration_filename",
    "member_b_registration_internal",
    "member_b_state_registration",
    "member_b_title_uk",
    "member_b_text_chars",
    "member_b_text_sha256",
    "member_b_render_sha256",
    "member_b_error",
    "normalized_text_equal",
    "render_equal",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalized(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\u00ad", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip().casefold()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inspect_variant(
    data: bytes, *, member: str, doc_id: str, year: int
) -> dict[str, Any]:
    """Open every PDF page and return content and parsing fingerprints."""
    result: dict[str, Any] = {
        "member": member,
        "source_file": PurePosixPath(member).name,
        "sha256": digest(data),
        "bytes": len(data),
        "readable": False,
        "pages": 0,
        "opened_pages": 0,
        "registration_filename": tables.registration_from_name(member),
        "registration_internal": "",
        "state_registration": "",
        "title_uk": "",
        "title_en": "",
        "text_chars": 0,
        "text_sha256": "",
        "render_sha256": "",
        "quality": None,
        "error": "",
    }
    document = None
    try:
        document = fitz.open(stream=data, filetype="pdf")
        result["pages"] = document.page_count
        text_parts: list[str] = []
        render_hasher = hashlib.sha256()
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            text_parts.append(tables.extractor.page_to_text(page))
            # At 72 dpi this is a deterministic visual comparison without
            # creating thousands of temporary PNG files.
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(1, 1), colorspace=fitz.csGRAY, alpha=False
            )
            render_hasher.update(f"{pixmap.width}x{pixmap.height}:".encode("ascii"))
            render_hasher.update(pixmap.samples)
            result["opened_pages"] = page_index + 1

        full_text = "\n".join(text_parts)
        stable_text = normalized(full_text)
        result["text_chars"] = len(full_text)
        result["text_sha256"] = digest(stable_text.encode("utf-8"))
        result["render_sha256"] = render_hasher.hexdigest()

        parsed = tables.parse_pdf_variant(
            data, member=member, doc_id=doc_id, year=year
        )
        result["registration_internal"] = str(
            parsed.record.get("registration_number", "") or ""
        )
        result["state_registration"] = str(
            parsed.record.get("state_registration_number", "") or ""
        )
        result["title_uk"] = str(parsed.record.get("title_uk", "") or "")
        result["title_en"] = str(parsed.record.get("title_en", "") or "")
        result["quality"] = parsed.quality
        result["readable"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if document is not None:
            document.close()
    return result


def same_nonempty(values: list[str]) -> bool:
    cleaned = [normalized(value) for value in values]
    return bool(cleaned) and all(cleaned) and len(set(cleaned)) == 1


def choose_best(variants: list[dict[str, Any]]) -> dict[str, Any] | None:
    readable = [variant for variant in variants if variant["readable"]]
    if not readable:
        return None
    return max(
        readable,
        key=lambda variant: (
            tuple(variant["quality"] or ()),
            variant["opened_pages"] == variant["pages"],
            variant["bytes"],
            variant["member"],
        ),
    )


def classify(variants: list[dict[str, Any]]) -> tuple[str, bool | None, str]:
    readable = [variant for variant in variants if variant["readable"]]
    if not readable:
        return "all_variants_unreadable", None, "ни одна копия не открывается"

    if len(readable) == 1:
        only = readable[0]
        filename_regs = [
            variant["registration_filename"]
            for variant in variants
            if variant["registration_filename"]
        ]
        internal = normalized(only["registration_internal"])
        filenames_agree = len({normalized(value) for value in filename_regs}) <= 1
        readable_matches_names = bool(internal) and all(
            normalized(value) == internal for value in filename_regs
        )
        if filenames_agree or readable_matches_names:
            return (
                "same_publication_one_variant_unreadable",
                True,
                "одна копия полностью прочитана; регистрационные номера копий согласуются",
            )
        return (
            "ambiguous_unreadable_variant",
            None,
            "нечитаемую копию нельзя отождествить по регистрационному номеру файла",
        )

    text_hashes = {variant["text_sha256"] for variant in readable}
    render_hashes = {variant["render_sha256"] for variant in readable}
    if len(text_hashes) == 1 and len(render_hashes) == 1:
        return (
            "same_publication_identical_text_and_render",
            True,
            "совпадают нормализованный текст и изображение каждой страницы",
        )
    if len(text_hashes) == 1:
        return (
            "same_publication_identical_text_render_differs",
            True,
            "весь нормализованный текст совпадает; отличается только визуальное представление",
        )

    registrations = [variant["registration_internal"] for variant in readable]
    states = [variant["state_registration"] for variant in readable]
    titles = [variant["title_uk"] or variant["title_en"] for variant in readable]
    if same_nonempty(registrations) and same_nonempty(titles):
        return (
            "same_publication_revised_pdf",
            True,
            "совпадают внутренний регистрационный номер и название; PDF имеет редакционные отличия",
        )
    if same_nonempty(registrations) and same_nonempty(states):
        return (
            "same_publication_revised_pdf",
            True,
            "совпадают внутренний и государственный регистрационные номера",
        )

    # Strong evidence of a real doc_id collision: both files are readable and
    # internally identify different records.  Such rows must not be collapsed.
    nonempty_regs = {normalized(value) for value in registrations if normalized(value)}
    nonempty_titles = {normalized(value) for value in titles if normalized(value)}
    if len(nonempty_regs) > 1 and len(nonempty_titles) > 1:
        return (
            "different_publications_same_doc_id",
            False,
            "различаются внутренние регистрационные номера, названия и содержимое",
        )
    return (
        "ambiguous_content_difference",
        None,
        "содержимое различается, но идентификаторов недостаточно для автоматического решения",
    )


def read_table_selections(csv_path: Path) -> dict[str, str]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            row["doc_id"].lower(): row.get("source_member", "")
            for row in csv.DictReader(handle)
        }


def member_fields(prefix: str, variant: dict[str, Any] | None) -> dict[str, Any]:
    variant = variant or {}
    return {
        f"{prefix}": variant.get("member", ""),
        f"{prefix}_sha256": variant.get("sha256", ""),
        f"{prefix}_bytes": variant.get("bytes", ""),
        f"{prefix}_readable": "yes" if variant.get("readable") else "no",
        f"{prefix}_pages": variant.get("pages", ""),
        f"{prefix}_opened_pages": variant.get("opened_pages", ""),
        f"{prefix}_registration_filename": variant.get("registration_filename", ""),
        f"{prefix}_registration_internal": variant.get("registration_internal", ""),
        f"{prefix}_state_registration": variant.get("state_registration", ""),
        f"{prefix}_title_uk": variant.get("title_uk", ""),
        f"{prefix}_text_chars": variant.get("text_chars", ""),
        f"{prefix}_text_sha256": variant.get("text_sha256", ""),
        f"{prefix}_render_sha256": variant.get("render_sha256", ""),
        f"{prefix}_error": variant.get("error", ""),
    }


def audit_year(
    year: int, archive_path: Path, table_path: Path
) -> list[dict[str, Any]]:
    selections = read_table_selections(table_path)
    rows: list[dict[str, Any]] = []
    with zipfile.ZipFile(archive_path) as archive:
        grouped, unparsed = tables.collect_archive_members(archive)
        if unparsed:
            raise RuntimeError(f"{archive_path}: PDF без doc_id: {len(unparsed)}")
        duplicate_groups = [
            (doc_id, members)
            for doc_id, members in sorted(grouped.items())
            if len(members) > 1
        ]
        for index, (doc_id, members) in enumerate(duplicate_groups, start=1):
            variants = [
                inspect_variant(
                    archive.read(member), member=member, doc_id=doc_id, year=year
                )
                for member in members
            ]
            classification, same_publication, basis = classify(variants)
            selected = choose_best(variants)
            table_member = selections.get(doc_id, "")
            readable = [variant for variant in variants if variant["readable"]]
            all_pages_opened = all(
                variant["readable"]
                and variant["opened_pages"] == variant["pages"]
                for variant in variants
            )
            selected_member = selected["member"] if selected else ""
            if same_publication is True:
                action = "keep_one_best_copy"
            elif same_publication is False:
                action = "keep_each_publication"
            else:
                action = "manual_review_do_not_collapse"
            row: dict[str, Any] = {
                "year": year,
                "doc_id": doc_id,
                "classification": classification,
                "same_publication": (
                    "yes" if same_publication is True
                    else "no" if same_publication is False
                    else "unknown"
                ),
                "decision_basis": basis,
                "variant_count": len(variants),
                "readable_count": len(readable),
                "all_pages_opened": "yes" if all_pages_opened else "no",
                "selected_member": selected_member,
                "selected_source_file": selected.get("source_file", "") if selected else "",
                "selected_registration_number": selected.get("registration_internal", "") if selected else "",
                "selected_state_registration_number": selected.get("state_registration", "") if selected else "",
                "selected_title_uk": selected.get("title_uk", "") if selected else "",
                "table_source_member": table_member,
                "table_selection_matches_audit": (
                    "yes" if table_member and table_member == selected_member else "no"
                ),
                "dataset_action": action,
            }
            row.update(member_fields("member_a", variants[0] if variants else None))
            row.update(member_fields("member_b", variants[1] if len(variants) > 1 else None))
            row["normalized_text_equal"] = (
                "yes"
                if len(readable) == len(variants)
                and len({variant["text_sha256"] for variant in readable}) == 1
                else "no"
            )
            row["render_equal"] = (
                "yes"
                if len(readable) == len(variants)
                and len({variant["render_sha256"] for variant in readable}) == 1
                else "no"
            )
            rows.append(row)
            if index % 50 == 0 or index == len(duplicate_groups):
                print(
                    f"  {year}: {index}/{len(duplicate_groups)} групп проверено",
                    flush=True,
                )
    return rows


def write_reports(rows: list[dict[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "duplicate_publications_audit_1991_2025.csv"
    json_path = output_dir / "duplicate_publications_audit_1991_2025.json"
    md_path = output_dir / "DUPLICATE_PUBLICATIONS_AUDIT_1991_2025.md"

    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    counts = Counter(str(row["classification"]) for row in rows)
    years = Counter(int(row["year"]) for row in rows)
    mismatches = [
        row for row in rows if row["table_selection_matches_audit"] != "yes"
    ]
    unresolved = [row for row in rows if row["same_publication"] == "unknown"]
    different = [row for row in rows if row["same_publication"] == "no"]
    unreadable = [
        row for row in rows if int(row["readable_count"]) < int(row["variant_count"])
    ]
    payload = {
        "generated_at_utc": utc_now(),
        "scope": {"start_year": 1991, "end_year": 2025},
        "duplicate_groups": len(rows),
        "variant_files": sum(int(row["variant_count"]) for row in rows),
        "classification_counts": dict(sorted(counts.items())),
        "year_counts": dict(sorted(years.items())),
        "groups_with_unreadable_variant": len(unreadable),
        "different_publication_collisions": len(different),
        "unresolved_groups": len(unresolved),
        "table_selection_mismatches": len(mismatches),
        "rows": rows,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# Полный аудит дублирующихся публикаций НРАТ, 1991–2025",
        "",
        f"Сформирован: {payload['generated_at_utc']}",
        "",
        f"Проверено групп с несколькими PDF: **{len(rows):,}**.",
        f"Открыто и проверено файлов-вариантов: **{payload['variant_files']:,}**.",
        f"Групп с нечитаемой копией: **{len(unreadable):,}**.",
        f"Разных публикаций под одним doc_id: **{len(different):,}**.",
        f"Нерешённых групп: **{len(unresolved):,}**.",
        f"Несовпадений выбранной версии с итоговой таблицей: **{len(mismatches):,}**.",
        "",
        "## Классификация",
        "",
        "| Категория | Групп |",
        "|---|---:|",
    ]
    lines.extend(f"| `{key}` | {value:,} |" for key, value in sorted(counts.items()))
    lines.extend(["", "## По годам", "", "| Год | Групп |", "|---:|---:|"])
    lines.extend(f"| {year} | {count:,} |" for year, count in sorted(years.items()))
    lines.extend(
        [
            "",
            "Подробные имена файлов, хеши, внутренние номера, заголовки, ошибки открытия и решение по каждой группе находятся в CSV/JSON-реестре.",
            "",
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=1991)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--archives-dir", type=Path, default=DEFAULT_ARCHIVES_DIR)
    parser.add_argument("--tables-dir", type=Path, default=DEFAULT_TABLES_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows: list[dict[str, Any]] = []
    for year in range(args.start_year, args.end_year + 1):
        archive_path = args.archives_dir / f"{year}.zip"
        table_path = args.tables_dir / f"output_{year}.csv"
        if not archive_path.exists():
            raise FileNotFoundError(archive_path)
        if not table_path.exists():
            raise FileNotFoundError(table_path)
        with zipfile.ZipFile(archive_path) as archive:
            grouped, _ = tables.collect_archive_members(archive)
            count = sum(len(members) > 1 for members in grouped.values())
        if count:
            print(f"{year}: найдено {count} групп вариантов", flush=True)
            rows.extend(audit_year(year, archive_path, table_path))
    write_reports(rows, args.output_dir)
    print(f"ВСЕГО: {len(rows)} групп вариантов проверено", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
