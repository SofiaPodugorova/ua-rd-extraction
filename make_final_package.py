"""Create one verified NRAT 1991-2025 delivery folder.

Large PDF ZIPs and CSVs are exposed through Windows hard links so the final
package behaves like a normal self-contained folder without consuming another
~46 GB on the same disk.  Reports and small error registries are copied.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import final_audit_1991_2025 as website_audit


REPO_DIR = Path(__file__).resolve().parent
WORKSPACE = REPO_DIR.parent
SOURCE_PDFS = WORKSPACE / "nrat pdfs"
SOURCE_TABLES = WORKSPACE / "outputs" / "nrat_excel_1991_2025" / "csv"
SOURCE_REPORTS = WORKSPACE / "outputs" / "nrat_excel_1991_2025" / "reports"
PACKAGE = WORKSPACE / "FINAL_NRAT_1991_2025"
PDF_DIR = PACKAGE / "01_PDF_DATASET_ZIP"
TABLE_DIR = PACKAGE / "02_FINAL_EXCEL_CSV"
REPORT_DIR = PACKAGE / "03_REPORTS"
SUMMARY_DIR = REPORT_DIR / "00_MAIN_SUMMARY"
NO_PDF_DIR = REPORT_DIR / "01_NO_PDF"
HTTP_500_DIR = REPORT_DIR / "02_HTTP_500"
LISTING_GAP_DIR = REPORT_DIR / "03_LISTING_GAPS"
DUPLICATE_DIR = REPORT_DIR / "04_DUPLICATES"
DAMAGED_PDF_DIR = REPORT_DIR / "05_DAMAGED_PDF"
EXTRACTION_DIR = REPORT_DIR / "06_EXTRACTION_WARNINGS"
ARCHIVE_EXTRA_DIR = REPORT_DIR / "07_ARCHIVE_EXTRAS"
TECHNICAL_DIR = REPORT_DIR / "90_TECHNICAL_VALIDATION"
ERROR_DIR = REPORT_DIR / "99_RAW_REPAIR_LOGS"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def link_file(source: Path, destination: Path) -> None:
    if destination.exists():
        if not os.path.samefile(source, destination):
            raise RuntimeError(f"Уже существует другой файл: {destination}")
        return
    os.link(source, destination)


def copy_file(source: Path, destination: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(source)
    shutil.copy2(source, destination)


def place_report(source: Path, destination: Path) -> None:
    """Move an old ungrouped package copy, then refresh it from source."""
    legacy = REPORT_DIR / source.name
    if legacy.exists() and legacy.resolve() != destination.resolve():
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            legacy.replace(destination)
        else:
            legacy.unlink()
    copy_file(source, destination)


def write_rows(path: Path, header: str, rows: list[tuple[object, ...]]) -> None:
    lines = [header]
    lines.extend("\t".join(str(value) for value in row) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def collect_grouped_details() -> dict[str, list]:
    no_pdf: dict[tuple[int, str], set[str]] = {}
    http_500: dict[tuple[int, str], set[str]] = {}
    listing_gaps: list[tuple[int, str, int]] = []
    archive_extras: list[tuple[int, str]] = []

    for year in range(1991, 2026):
        progress_path = SOURCE_PDFS / f"_repair_progress_{year}.json"
        if progress_path.exists():
            progress = load_json(progress_path)
            for date, day in progress.get("days", {}).items():
                for doc_id in day.get("no_pdf_doc_ids", []):
                    no_pdf.setdefault((year, doc_id.lower()), set()).add(date)
                for doc_id in day.get("server500_doc_ids", []):
                    http_500.setdefault((year, doc_id.lower()), set()).add(date)

        report = load_json(SOURCE_PDFS / f"_repair_report_{year}.json")
        gaps = website_audit.persistent_count_mismatches(
            report, SOURCE_PDFS / f"_repair_errors_{year}.txt"
        )
        listing_gaps.extend((year, date, gap) for date, gap in gaps.items())

        site_ids, _, _ = website_audit.repair_sets(progress_path)
        _, archive_ids, _ = website_audit.archive_ids(SOURCE_PDFS / f"{year}.zip")
        archive_extras.extend((year, doc_id) for doc_id in sorted(archive_ids - site_ids))

    no_pdf_rows = [
        (year, ";".join(sorted(dates)), doc_id)
        for (year, doc_id), dates in sorted(no_pdf.items())
    ]
    http_500_rows = [
        (year, ";".join(sorted(dates)), doc_id)
        for (year, doc_id), dates in sorted(http_500.items())
    ]
    return {
        "no_pdf": no_pdf_rows,
        "http_500": http_500_rows,
        "listing_gaps": sorted(listing_gaps),
        "archive_extras": sorted(archive_extras),
    }


def main() -> int:
    final_audit = load_json(SOURCE_REPORTS / "final_audit_1991_2025.json")
    duplicate_audit = load_json(
        SOURCE_REPORTS / "duplicate_publications_audit_1991_2025.json"
    )
    totals = final_audit["totals"]

    site = int(totals["site_publications"])
    current_pdf = int(totals["pdf_doc_ids_in_current_site_year"])
    no_pdf = int(totals["known_no_pdf_doc_ids"])
    http_500 = int(totals["known_http_500_doc_ids"])
    listing_gap = int(totals["known_listing_gap_without_doc_id"])
    known_unavailable = no_pdf + http_500 + listing_gap
    archive_entries = int(totals["archive_pdf_entries"])
    archive_unique = int(totals["archive_unique_pdf_doc_ids"])
    outside_current_year = int(
        totals["archive_pdf_doc_ids_outside_current_site_year"]
    )
    table_rows = int(totals["final_table_rows"])
    table_unique = int(totals["final_table_unique_doc_ids"])
    duplicate_groups = int(duplicate_audit["duplicate_groups"])
    variant_files = int(duplicate_audit["variant_files"])
    duplicate_extra_files = variant_files - duplicate_groups

    checks = {
        "website_accounting": current_pdf + no_pdf + http_500 + listing_gap == site,
        "archive_accounting": current_pdf + outside_current_year == archive_unique,
        "physical_pdf_accounting": archive_unique + duplicate_extra_files == archive_entries,
        "pdf_to_excel_rows": archive_unique == table_rows,
        "pdf_to_excel_unique_ids": archive_unique == table_unique,
        "duplicate_audit_resolved": (
            int(duplicate_audit["different_publication_collisions"]) == 0
            and int(duplicate_audit["unresolved_groups"]) == 0
            and int(duplicate_audit["table_selection_mismatches"]) == 0
        ),
    }
    if not all(checks.values()):
        raise RuntimeError(f"Финальные равенства не прошли проверку: {checks}")

    legacy_error_dir = REPORT_DIR / "known_scraping_errors"
    if legacy_error_dir.exists() and not ERROR_DIR.exists():
        legacy_error_dir.replace(ERROR_DIR)

    report_directories = (
        REPORT_DIR,
        SUMMARY_DIR,
        NO_PDF_DIR,
        HTTP_500_DIR,
        LISTING_GAP_DIR,
        DUPLICATE_DIR,
        DAMAGED_PDF_DIR,
        EXTRACTION_DIR,
        ARCHIVE_EXTRA_DIR,
        TECHNICAL_DIR,
        ERROR_DIR,
    )
    for directory in (PDF_DIR, TABLE_DIR, *report_directories):
        directory.mkdir(parents=True, exist_ok=True)

    pdf_sources = [SOURCE_PDFS / f"{year}.zip" for year in range(1991, 2026)]
    table_sources = [
        SOURCE_TABLES / f"output_{year}.csv" for year in range(1991, 2026)
    ]
    for source in pdf_sources + table_sources:
        if not source.exists():
            raise FileNotFoundError(source)
    for source in pdf_sources:
        link_file(source, PDF_DIR / source.name)
    for source in table_sources:
        link_file(source, TABLE_DIR / source.name)

    checks["package_pdf_files_exact"] = (
        len(list(PDF_DIR.glob("*.zip"))) == 35
        and all(os.path.samefile(source, PDF_DIR / source.name) for source in pdf_sources)
    )
    checks["package_table_files_exact"] = (
        len(list(TABLE_DIR.glob("output_*.csv"))) == 35
        and all(
            os.path.samefile(source, TABLE_DIR / source.name)
            for source in table_sources
        )
    )
    if not checks["package_pdf_files_exact"] or not checks["package_table_files_exact"]:
        raise RuntimeError("Файлы финального пакета не совпадают с проверенными исходниками")

    report_destinations = {
        "FINAL_AUDIT_1991_2025.md": SUMMARY_DIR,
        "final_audit_1991_2025.csv": SUMMARY_DIR,
        "final_audit_1991_2025.json": SUMMARY_DIR,
        "DUPLICATE_PUBLICATIONS_AUDIT_1991_2025.md": DUPLICATE_DIR,
        "duplicate_publications_audit_1991_2025.csv": DUPLICATE_DIR,
        "duplicate_publications_audit_1991_2025.json": DUPLICATE_DIR,
        "quality_issues_1991_2025.csv": EXTRACTION_DIR,
        "manifest_1991_2025.csv": TECHNICAL_DIR,
        "schema_135_columns.json": TECHNICAL_DIR,
        "summary_1991_2025.json": TECHNICAL_DIR,
        "validation_1991_2025.json": TECHNICAL_DIR,
    }
    for name, destination_dir in report_destinations.items():
        place_report(SOURCE_REPORTS / name, destination_dir / name)

    error_sources = sorted(SOURCE_PDFS.glob("_repair_errors_*.txt"))
    error_sources += sorted(SOURCE_PDFS.glob("_repair_report_*.json"))
    error_sources += [
        SOURCE_PDFS / "_repair_incomplete_dates_1991_2025.txt",
        SOURCE_PDFS / "_repair_summary_1991_2025.json",
    ]
    for source in error_sources:
        copy_file(source, ERROR_DIR / source.name)

    details = collect_grouped_details()
    if len(details["no_pdf"]) != no_pdf:
        raise RuntimeError(f"NO_PDF: ожидалось {no_pdf}, найдено {len(details['no_pdf'])}")
    if len(details["http_500"]) != http_500:
        raise RuntimeError(
            f"HTTP 500: ожидалось {http_500}, найдено {len(details['http_500'])}"
        )
    if sum(row[2] for row in details["listing_gaps"]) != listing_gap:
        raise RuntimeError("Не совпала сумма разрывов выдачи")
    if len(details["archive_extras"]) != outside_current_year:
        raise RuntimeError("Не совпало число архивных PDF вне текущей выдачи")

    write_rows(
        NO_PDF_DIR / f"NO_PDF_{no_pdf}.txt",
        "year\tdate\tdoc_id",
        details["no_pdf"],
    )
    write_rows(
        HTTP_500_DIR / f"HTTP_500_{http_500}.txt",
        "year\tdate\tdoc_id",
        details["http_500"],
    )
    write_rows(
        LISTING_GAP_DIR / f"LISTING_GAPS_{listing_gap}.txt",
        "year\tdate\tmissing_publications_without_doc_id",
        details["listing_gaps"],
    )
    write_rows(
        ARCHIVE_EXTRA_DIR / f"ARCHIVE_EXTRAS_{outside_current_year}.txt",
        "year\tdoc_id",
        details["archive_extras"],
    )

    quality_rows: list[dict[str, str]] = []
    with (SOURCE_REPORTS / "quality_issues_1991_2025.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        quality_rows = list(csv.DictReader(handle))
    failed_rows = [row for row in quality_rows if row["extraction_status"] == "failed"]
    write_rows(
        DAMAGED_PDF_DIR / f"UNREADABLE_UNIQUE_PDF_{len(failed_rows)}.txt",
        "year\tdoc_id\tsource_member\terror",
        [
            (row["year"], row["doc_id"], row["source_member"], row["extraction_error"])
            for row in failed_rows
        ],
    )
    bad_duplicate_rows = [
        row
        for row in duplicate_audit["rows"]
        if int(row["readable_count"]) < int(row["variant_count"])
    ]
    write_rows(
        DAMAGED_PDF_DIR
        / f"DAMAGED_DUPLICATE_WITH_GOOD_COPY_{len(bad_duplicate_rows)}.txt",
        "year\tdoc_id\tdamaged_member\tdamaged_error\tselected_good_member",
        [
            (
                row["year"],
                row["doc_id"],
                row["member_a"] if row["member_a_readable"] == "no" else row["member_b"],
                row["member_a_error"] if row["member_a_readable"] == "no" else row["member_b_error"],
                row["selected_member"],
            )
            for row in bad_duplicate_rows
        ],
    )

    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    collection_percent = current_pdf / site if site else 1.0
    grouped_summary = {
        "generated_at_utc": generated,
        "no_pdf": no_pdf,
        "http_500": http_500,
        "listing_gap_without_doc_id": listing_gap,
        "known_without_available_pdf_total": known_unavailable,
        "duplicate_groups": duplicate_groups,
        "duplicate_extra_files": duplicate_extra_files,
        "different_publication_collisions": int(
            duplicate_audit["different_publication_collisions"]
        ),
        "damaged_unique_pdf_without_alternative": len(failed_rows),
        "damaged_duplicate_copy_with_good_alternative": len(bad_duplicate_rows),
        "extraction_warning_rows": int(totals["final_table_warning_rows"]),
        "archive_extras_outside_current_site_year": outside_current_year,
        "checks": {
            "no_pdf_register_rows_match": len(details["no_pdf"]) == no_pdf,
            "http_500_register_rows_match": len(details["http_500"]) == http_500,
            "listing_gap_register_sum_matches": sum(
                row[2] for row in details["listing_gaps"]
            )
            == listing_gap,
            "duplicate_register_rows_match": len(duplicate_audit["rows"])
            == duplicate_groups,
            "archive_extra_register_rows_match": len(details["archive_extras"])
            == outside_current_year,
        },
    }
    (SUMMARY_DIR / "ERROR_GROUPS_SUMMARY.json").write_text(
        json.dumps(grouped_summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    grouped_readme = f"""# Главные группы ошибок и исключений

| Папка | Что внутри | Количество |
|---|---|---:|
| `01_NO_PDF` | Сайт показывает публикацию, но сообщает, что PDF отсутствует | {no_pdf:,} |
| `02_HTTP_500` | `doc_id`, для которых PDF возвращает HTTP 500 | {http_500:,} |
| `03_LISTING_GAPS` | Позиции есть в счётчике, но сайт не отдаёт их `doc_id` | {listing_gap:,} |
| `04_DUPLICATES` | Группы с двумя PDF-вариантами одного `doc_id` | {duplicate_groups:,} |
| `05_DAMAGED_PDF` | Уникальный повреждённый PDF без замены | {len(failed_rows):,} |
| `05_DAMAGED_PDF` | Повреждённая копия дубля с исправной заменой | {len(bad_duplicate_rows):,} |
| `06_EXTRACTION_WARNINGS` | Строки таблиц с предупреждениями извлечения | {int(totals['final_table_warning_rows']):,} |
| `07_ARCHIVE_EXTRAS` | PDF, сохранённые в архиве, но отсутствующие в текущем годовом списке сайта | {outside_current_year:,} |
| `90_TECHNICAL_VALIDATION` | Схема, манифест и строгая сверка ZIP ↔ CSV | — |
| `99_RAW_REPAIR_LOGS` | Исходные годовые журналы и repair-отчёты | {len(error_sources):,} файлов |

`NO_PDF + HTTP 500 + LISTING_GAPS = {no_pdf:,} + {http_500:,} + {listing_gap:,} = {known_unavailable:,}`
позиций сайта без доступного PDF.

Дубли не являются пропущенными публикациями: все {duplicate_groups:,} группы
проверены, настоящих разных публикаций под одним `doc_id` нет. Для каждой
публикации в итоговой таблице оставлена одна правильная версия.
"""
    (SUMMARY_DIR / "READ_ME_FIRST.md").write_text(grouped_readme, encoding="utf-8")

    report = f"""# Финальный отчёт НРАТ, 1991–2025

Сформирован: {generated}

## Короткий ответ

**Учёт публикаций сайта полный: ДА.** Все {site:,} позиций годовых счётчиков
либо представлены собранным PDF, либо объяснены сохранённой известной ошибкой.

**Все доступные PDF внесены в итоговые таблицы: ДА.** В канонических архивах
{archive_unique:,} уникальных `doc_id`, и в итоговых CSV ровно {table_rows:,}
строк и {table_unique:,} уникальных `doc_id`.

**У каждой публикации сайта есть собранный PDF: НЕТ.** Для {known_unavailable:,}
позиций сайт не дал пригодный PDF: {no_pdf:,} имеют подтверждённый статус
`no_pdf`, {http_500:,} возвращают HTTP 500, ещё {listing_gap:,} входят в
счётчик сайта, но отсутствуют в списке выдачи. Это известные исключения сайта,
а не незамеченные пропуски скрипта.

## Итоговые числа

| Показатель | Количество |
|---|---:|
| Публикаций показывает сайт | {site:,} |
| PDF `doc_id`, собранных из текущей годовой выдачи сайта | {current_pdf:,} |
| Без PDF (`no_pdf`) | {no_pdf:,} |
| Известные HTTP 500 | {http_500:,} |
| Разрыв выдачи: есть в счётчике, нет `doc_id` | {listing_gap:,} |
| Всего известных позиций без доступного PDF | {known_unavailable:,} |
| Уникальных `doc_id` в канонических PDF-архивах | {archive_unique:,} |
| Дополнительно сохранено вне текущего года сайта | {outside_current_year:,} |
| Физических PDF-вхождений в ZIP | {archive_entries:,} |
| Групп с двумя PDF-вариантами одного `doc_id` | {duplicate_groups:,} |
| Дополнительных файлов-вариантов | {duplicate_extra_files:,} |
| Настоящих разных публикаций, ошибочно объединённых как дубль | {int(duplicate_audit['different_publication_collisions']):,} |
| Строк в итоговых таблицах | {table_rows:,} |
| Уникальных `doc_id` в итоговых таблицах | {table_unique:,} |
| Строк таблиц с предупреждениями извлечения | {int(totals['final_table_warning_rows']):,} |
| Строк таблиц с ошибкой извлечения | {int(totals['final_table_failed_rows']):,} |

## Контрольные формулы

1. Сайт полностью объяснён:

   `{current_pdf:,} PDF + {no_pdf:,} no_pdf + {http_500:,} HTTP 500 + {listing_gap:,} разрыв выдачи = {site:,}`.

2. Канонический PDF-набор:

   `{current_pdf:,} из текущей выдачи + {outside_current_year:,} ранее сохранённых = {archive_unique:,} уникальных PDF doc_id`.

3. Физические варианты в ZIP:

   `{archive_unique:,} уникальных doc_id + {duplicate_extra_files:,} дополнительных вариантов = {archive_entries:,} PDF-вхождений`.

4. PDF полностью перенесены в таблицы:

   `{archive_unique:,} уникальных PDF doc_id = {table_rows:,} строк CSV = {table_unique:,} уникальных doc_id CSV`.

Доля текущих публикаций сайта с полученным PDF: **{collection_percent:.2%}**.
Полнота учёта с известными исключениями: **100%**.
Полнота соответствия уникальных PDF итоговым таблицам: **100%**.

## Дубли

Проверены все {duplicate_groups:,} группы ({variant_files:,} файла-варианта).
В {int(duplicate_audit['classification_counts'].get('same_publication_identical_text_and_render', 0)):,}
группах совпали текст и отрисовка всех страниц. В одной группе 2016 года одна
копия повреждена, но в наборе данных и таблице выбрана исправная копия.
Настоящих разных публикаций под одним `doc_id` и нерешённых случаев нет.

## Единственный PDF, который не удалось разобрать в таблицу

В PDF-наборе сохранена публикация 2023 года с `doc_id`
`8c2291621873bda3bf520316f743a621`, файл `0223U002335_...pdf`. PDF повреждён
и не открывается (`FileDataError`). Строка для него присутствует в таблице,
но поля публикации не извлечены; статус строки — `failed`. Другой копии этого
`doc_id` в архивах нет.

## Структура финального пакета

- `01_PDF_DATASET_ZIP` — 35 канонических ZIP, по одному на каждый год;
- `02_FINAL_EXCEL_CSV` — 35 итоговых CSV, открывающихся в Excel;
- `03_REPORTS/00_MAIN_SUMMARY` — краткий итог и годовая сверка;
- `03_REPORTS/01_NO_PDF` — полный реестр `no_pdf`;
- `03_REPORTS/02_HTTP_500` — полный реестр HTTP 500;
- `03_REPORTS/03_LISTING_GAPS` — разрывы между счётчиком и выдачей;
- `03_REPORTS/04_DUPLICATES` — полный аудит PDF-вариантов;
- `03_REPORTS/05_DAMAGED_PDF` — повреждённые PDF с заменой и без неё;
- `03_REPORTS/06_EXTRACTION_WARNINGS` — предупреждения и ошибки таблиц;
- `03_REPORTS/07_ARCHIVE_EXTRAS` — сохранённые PDF вне текущей выдачи сайта;
- `03_REPORTS/90_TECHNICAL_VALIDATION` — схема, манифест и строгая проверка;
- `03_REPORTS/99_RAW_REPAIR_LOGS` — исходные годовые repair-отчёты и логи.

PDF-ZIP и CSV в этом пакете являются жёсткими ссылками на проверенные исходные
файлы на том же диске. Это нормальные открываемые файлы, но они не занимают
второй раз около 46 ГБ дискового пространства.
"""
    (PACKAGE / "FINAL_REPORT_1991_2025.md").write_text(report, encoding="utf-8")

    machine_report = {
        "generated_at_utc": generated,
        "period": "1991-2025",
        "website": {
            "shown": site,
            "collected_current_year_pdf_doc_ids": current_pdf,
            "known_no_pdf": no_pdf,
            "known_http_500": http_500,
            "known_listing_gap_without_doc_id": listing_gap,
            "known_without_available_pdf_total": known_unavailable,
            "accounted_total": current_pdf + known_unavailable,
            "accounting_complete": checks["website_accounting"],
        },
        "pdf_dataset": {
            "zip_files": len(pdf_sources),
            "physical_entries": archive_entries,
            "unique_doc_ids": archive_unique,
            "outside_current_site_year": outside_current_year,
        },
        "duplicates": {
            "groups": duplicate_groups,
            "variant_files": variant_files,
            "extra_variant_files": duplicate_extra_files,
            "different_publication_collisions": int(
                duplicate_audit["different_publication_collisions"]
            ),
            "unresolved_groups": int(duplicate_audit["unresolved_groups"]),
            "table_selection_mismatches": int(
                duplicate_audit["table_selection_mismatches"]
            ),
        },
        "final_excel_csv": {
            "files": len(table_sources),
            "rows": table_rows,
            "unique_doc_ids": table_unique,
            "warning_rows": int(totals["final_table_warning_rows"]),
            "failed_rows": int(totals["final_table_failed_rows"]),
        },
        "checks": checks,
        "conclusion": {
            "all_website_positions_accounted_for": True,
            "all_available_pdfs_collected": True,
            "every_website_position_has_pdf": False,
            "all_unique_archived_pdfs_have_excel_row": True,
            "all_excel_rows_fully_parsed": False,
        },
    }
    (PACKAGE / "FINAL_REPORT_1991_2025.json").write_text(
        json.dumps(machine_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Пакет: {PACKAGE}")
    print(f"PDF ZIP: {len(pdf_sources)}")
    print(f"Excel CSV: {len(table_sources)}")
    print(f"Подробных файлов ошибок: {len(error_sources)}")
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
