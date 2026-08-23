"""Объединить коллекцию НРАТ в один проверенный ZIP на каждый год.

Активным результатом становятся ``1991.zip`` ... ``2025.zip``. Исходные
части перемещаются в отдельный резервный каталог только после проверки нового
архива. Полнота сравнивается по уникальным ``doc_id``; разные байтовые версии
одного ``doc_id`` не удаляются, а сохраняются в ``_variants``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DOC_ID_RE = re.compile(r"([0-9a-f]{32})", re.IGNORECASE)
DATE_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
YEAR_ARCHIVE_RE = re.compile(r"^(\d{4})(?:$|[-_.])", re.IGNORECASE)


def default_output_dir() -> Path:
    cwd = Path.cwd().resolve()
    candidates = (cwd / "nrat pdfs", cwd.parent / "nrat pdfs")
    return next((path for path in candidates if path.is_dir()), candidates[1])


@dataclass(frozen=True)
class SourceRef:
    kind: str
    source_path: Path
    member: str | None
    label: str
    basename: str
    date: str | None
    doc_id: str | None


def doc_id_from_name(name: str) -> str | None:
    matches = DOC_ID_RE.findall(Path(name).name)
    return matches[-1].lower() if matches else None


def date_from_name(name: str, year: int) -> str | None:
    match = DATE_RE.search(name.replace("\\", "/"))
    if match and match.group(1).startswith(f"{year}-"):
        return match.group(1)
    return None


def safe_basename(name: str, doc_id: str) -> str:
    basename = re.sub(r"[^\w.()\-]+", "_", Path(name).name, flags=re.UNICODE)
    if not basename.lower().endswith(".pdf"):
        basename += ".pdf"
    if doc_id not in basename.lower():
        basename = f"{Path(basename).stem}_{doc_id}.pdf"
    return basename


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


class Consolidator:
    def __init__(
        self,
        output_dir: Path,
        start_year: int,
        end_year: int,
        dry_run: bool = False,
    ) -> None:
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure:
                reconfigure(encoding="utf-8", errors="replace")
        self.output_dir = output_dir.expanduser().resolve()
        self.start_year = start_year
        self.end_year = end_year
        self.dry_run = dry_run
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.backup_dir = self.output_dir / f"_pre_consolidation_{stamp}"
        self.report_path = (
            self.output_dir / f"_consolidation_report_{start_year}_{end_year}.json"
        )
        self.results: dict[str, Any] = {}

    def archives_for_year(self, year: int) -> list[Path]:
        result = []
        for path in self.output_dir.glob(f"{year}*.zip"):
            match = YEAR_ARCHIVE_RE.match(path.stem)
            if path.is_file() and match and int(match.group(1)) == year:
                result.append(path.resolve())
        return sorted(result)

    def folders_for_year(self, year: int) -> list[Path]:
        candidates = (
            self.output_dir / str(year),
            self.output_dir / ".nrat_work" / str(year),
            self.output_dir / ".nrat_repair_work" / str(year),
        )
        return [path.resolve() for path in candidates if path.is_dir()]

    def inventory(
        self, year: int, archives: list[Path], folders: list[Path]
    ) -> tuple[dict[str, list[SourceRef]], list[SourceRef], int]:
        groups: dict[str, list[SourceRef]] = defaultdict(list)
        unparsed: list[SourceRef] = []
        physical = 0
        for archive_path in archives:
            if not zipfile.is_zipfile(archive_path):
                raise RuntimeError(f"Не является ZIP: {archive_path}")
            with zipfile.ZipFile(archive_path) as archive:
                for info in archive.infolist():
                    if info.is_dir() or not info.filename.lower().endswith(".pdf"):
                        continue
                    physical += 1
                    doc_id = doc_id_from_name(info.filename)
                    ref = SourceRef(
                        kind="zip",
                        source_path=archive_path,
                        member=info.filename,
                        label=f"{archive_path.name}!{info.filename}",
                        basename=Path(info.filename).name,
                        date=date_from_name(info.filename, year),
                        doc_id=doc_id,
                    )
                    (groups[doc_id] if doc_id else unparsed).append(ref)
        for folder in folders:
            for path in sorted(folder.rglob("*.pdf")):
                physical += 1
                doc_id = doc_id_from_name(path.name)
                ref = SourceRef(
                    kind="file",
                    source_path=path.resolve(),
                    member=None,
                    label=str(path.resolve()),
                    basename=path.name,
                    date=date_from_name(str(path), year),
                    doc_id=doc_id,
                )
                (groups[doc_id] if doc_id else unparsed).append(ref)
        return dict(groups), unparsed, physical

    @staticmethod
    def read_ref(ref: SourceRef, handles: dict[Path, zipfile.ZipFile]) -> bytes:
        if ref.kind == "zip":
            return handles[ref.source_path].read(ref.member or "")
        return ref.source_path.read_bytes()

    @staticmethod
    def id_digest(ids: set[str]) -> str:
        joined = "\n".join(sorted(ids)).encode("ascii")
        return hashlib.sha256(joined).hexdigest()

    @staticmethod
    def unique_arcname(
        year: int,
        date: str | None,
        basename: str,
        doc_id: str,
        used: set[str],
    ) -> str:
        folder = f"{year}/{date[:7]}/{date}" if date else f"{year}/_undated"
        candidate = f"{folder}/{safe_basename(basename, doc_id)}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        candidate = f"{folder}/{doc_id}_{safe_basename(basename, doc_id)}"
        suffix = 2
        while candidate in used:
            candidate = (
                f"{folder}/{doc_id}_{suffix}_{safe_basename(basename, doc_id)}"
            )
            suffix += 1
        used.add(candidate)
        return candidate

    def create_empty_archive(self, year: int, final_path: Path) -> dict[str, Any]:
        temporary = final_path.with_suffix(".zip.tmp")
        manifest = {
            "version": 1,
            "year": year,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_pdf_entries": 0,
            "unique_doc_ids": 0,
            "note": "НРАТ подтвердил отсутствие публикаций за этот год",
        }
        with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
            archive.writestr(
                f"{year}/_consolidation_manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        os.replace(temporary, final_path)
        return {
            "action": "created_empty",
            "source_pdf_entries": 0,
            "source_unique_doc_ids": 0,
            "output_pdf_entries": 0,
            "output_unique_doc_ids": 0,
            "variants_preserved": 0,
            "identical_duplicate_entries_removed": 0,
        }

    def merge_archive(
        self,
        year: int,
        final_path: Path,
        archives: list[Path],
        groups: dict[str, list[SourceRef]],
        unparsed: list[SourceRef],
        source_physical: int,
    ) -> dict[str, Any]:
        if unparsed:
            examples = [ref.label for ref in unparsed[:10]]
            raise RuntimeError(
                f"{year}: у {len(unparsed)} PDF не распознан doc_id: {examples}"
            )
        temporary = final_path.with_suffix(".zip.tmp")
        temporary.unlink(missing_ok=True)
        handles = {path: zipfile.ZipFile(path) for path in archives}
        aliases: list[dict[str, Any]] = []
        variants = 0
        identical_duplicates = 0
        used_arcnames: set[str] = set()
        output_entries = 0
        started = time.monotonic()
        try:
            with zipfile.ZipFile(
                temporary,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=1,
                allowZip64=True,
            ) as output:
                for index, doc_id in enumerate(sorted(groups), 1):
                    refs = groups[doc_id]
                    seen_hashes: dict[str, tuple[SourceRef, str]] = {}
                    alias_labels: list[str] = []
                    for ref in refs:
                        data = self.read_ref(ref, handles)
                        if len(data) <= 4 or not data.startswith(b"%PDF"):
                            raise RuntimeError(f"Некорректный PDF: {ref.label}")
                        digest = hashlib.sha256(data).hexdigest()
                        alias_labels.append(ref.label)
                        if digest in seen_hashes:
                            identical_duplicates += 1
                            continue
                        if seen_hashes:
                            variants += 1
                        arcname = self.unique_arcname(
                            year, ref.date, ref.basename, doc_id, used_arcnames
                        )
                        if seen_hashes:
                            date_folder = (
                                f"{year}/{ref.date[:7]}/{ref.date}"
                                if ref.date
                                else f"{year}/_undated"
                            )
                            arcname = (
                                f"{date_folder}/_variants/{doc_id}/"
                                f"{len(seen_hashes) + 1}_{Path(arcname).name}"
                            )
                            used_arcnames.add(arcname)
                        output.writestr(arcname, data)
                        output_entries += 1
                        seen_hashes[digest] = (ref, arcname)
                    if len(refs) > 1:
                        aliases.append(
                            {
                                "doc_id": doc_id,
                                "source_entries": alias_labels,
                                "distinct_content_versions": len(seen_hashes),
                                "output_entries": [
                                    arcname for _, arcname in seen_hashes.values()
                                ],
                            }
                        )
                    if index % 500 == 0 or index == len(groups):
                        elapsed = max(time.monotonic() - started, 0.001)
                        print(
                            f"  {year}: {index}/{len(groups)} doc_id, "
                            f"{output_entries} PDF, {index / elapsed:.1f} doc_id/сек",
                            flush=True,
                        )
                manifest = {
                    "version": 1,
                    "year": year,
                    "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    "source_archives": [path.name for path in archives],
                    "source_pdf_entries": source_physical,
                    "source_unique_doc_ids": len(groups),
                    "source_doc_ids_sha256": self.id_digest(set(groups)),
                    "output_pdf_entries": output_entries,
                    "output_unique_doc_ids": len(groups),
                    "identical_duplicate_entries_removed": identical_duplicates,
                    "content_variants_preserved": variants,
                    "duplicate_aliases": aliases,
                }
                output.writestr(
                    f"{year}/_consolidation_manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                )
        finally:
            for handle in handles.values():
                handle.close()

        validation = self.validate_archive(temporary, set(groups))
        if validation["bad_member"] is not None:
            raise RuntimeError(
                f"{year}: CRC-проверка не пройдена: {validation['bad_member']}"
            )
        os.replace(temporary, final_path)
        return {
            "action": "merged",
            "source_pdf_entries": source_physical,
            "source_unique_doc_ids": len(groups),
            "source_doc_ids_sha256": self.id_digest(set(groups)),
            "output_pdf_entries": validation["physical_pdf_entries"],
            "output_unique_doc_ids": validation["unique_doc_ids"],
            "output_doc_ids_sha256": validation["doc_ids_sha256"],
            "variants_preserved": variants,
            "identical_duplicate_entries_removed": identical_duplicates,
            "bad_member": None,
        }

    def validate_archive(
        self, path: Path, expected_ids: set[str] | None = None
    ) -> dict[str, Any]:
        with zipfile.ZipFile(path) as archive:
            infos = [
                info
                for info in archive.infolist()
                if not info.is_dir() and info.filename.lower().endswith(".pdf")
            ]
            ids = {
                doc_id
                for info in infos
                for doc_id in [doc_id_from_name(info.filename)]
                if doc_id
            }
            if expected_ids is not None and ids != expected_ids:
                missing = sorted(expected_ids - ids)[:20]
                extra = sorted(ids - expected_ids)[:20]
                raise RuntimeError(
                    f"{path.name}: doc_id не совпали; missing={missing}, extra={extra}"
                )
            print(f"  CRC-проверка {path.name}...", flush=True)
            bad_member = archive.testzip()
        return {
            "physical_pdf_entries": len(infos),
            "unique_doc_ids": len(ids),
            "doc_ids_sha256": self.id_digest(ids),
            "bad_member": bad_member,
        }

    def move_sources_to_backup(
        self,
        year: int,
        archives: list[Path],
        folders: list[Path],
        canonical: Path,
    ) -> list[str]:
        moved: list[str] = []
        year_backup = self.backup_dir / str(year)
        for source in archives:
            if source == canonical:
                continue
            year_backup.mkdir(parents=True, exist_ok=True)
            target = year_backup / source.name
            if target.exists():
                raise RuntimeError(f"Резервный файл уже существует: {target}")
            shutil.move(str(source), str(target))
            moved.append(str(target.relative_to(self.output_dir)))
        for folder in folders:
            relative_label = folder.relative_to(self.output_dir)
            target = year_backup / "folders" / relative_label
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise RuntimeError(f"Резервная папка уже существует: {target}")
            shutil.move(str(folder), str(target))
            moved.append(str(target.relative_to(self.output_dir)))
        return moved

    def update_repair_report(self, year: int, canonical: Path) -> None:
        path = self.output_dir / f"_repair_report_{year}.json"
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as handle:
                report = json.load(handle)
            if not isinstance(report, dict):
                return
            report["archives_used"] = [canonical.name]
            report["consolidated_archive"] = canonical.name
            report["consolidated_at_utc"] = datetime.now(timezone.utc).isoformat()
            atomic_json(path, report)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"  ⚠️ Не обновлён {path.name}: {exc}", flush=True)

    def process_year(self, year: int) -> dict[str, Any]:
        canonical = (self.output_dir / f"{year}.zip").resolve()
        archives = self.archives_for_year(year)
        folders = self.folders_for_year(year)
        groups, unparsed, source_physical = self.inventory(year, archives, folders)
        print(
            f"\n{year}: ZIP={len(archives)}, папок={len(folders)}, "
            f"PDF={source_physical}, unique doc_id={len(groups)}, "
            f"без doc_id={len(unparsed)}",
            flush=True,
        )
        if self.dry_run:
            return {
                "action": "dry_run",
                "source_archives": [path.name for path in archives],
                "source_folders": [str(path) for path in folders],
                "source_pdf_entries": source_physical,
                "source_unique_doc_ids": len(groups),
                "unparsed_pdf_entries": len(unparsed),
            }
        if unparsed:
            raise RuntimeError(f"{year}: есть PDF без распознанного doc_id")

        has_folder_pdfs = any(
            any(folder.rglob("*.pdf"))
            for folder in folders
        )
        if not archives and not groups:
            result = self.create_empty_archive(year, canonical)
        elif len(archives) == 1 and not has_folder_pdfs:
            source = archives[0]
            validation = self.validate_archive(source, set(groups))
            if validation["bad_member"] is not None:
                raise RuntimeError(
                    f"{year}: повреждённый файл в {source.name}: "
                    f"{validation['bad_member']}"
                )
            if source != canonical:
                if canonical.exists():
                    raise RuntimeError(f"Целевой архив уже существует: {canonical}")
                os.replace(source, canonical)
            result = {
                "action": "validated_and_renamed",
                "source_pdf_entries": source_physical,
                "source_unique_doc_ids": len(groups),
                "source_doc_ids_sha256": self.id_digest(set(groups)),
                "output_pdf_entries": validation["physical_pdf_entries"],
                "output_unique_doc_ids": validation["unique_doc_ids"],
                "output_doc_ids_sha256": validation["doc_ids_sha256"],
                "variants_preserved": 0,
                "identical_duplicate_entries_removed": 0,
                "bad_member": None,
            }
            archives = [canonical if path == source else path for path in archives]
        else:
            if canonical in archives:
                raise RuntimeError(
                    f"{year}: {canonical.name} уже существует вместе с другими источниками"
                )
            result = self.merge_archive(
                year,
                canonical,
                archives,
                groups,
                unparsed,
                source_physical,
            )

        moved = self.move_sources_to_backup(year, archives, folders, canonical)
        result.update(
            {
                "canonical_archive": canonical.name,
                "canonical_size_bytes": canonical.stat().st_size,
                "source_archives": [path.name for path in archives],
                "source_folders": [str(path) for path in folders],
                "backup_items": moved,
            }
        )
        self.update_repair_report(year, canonical)
        print(
            f"✅ {year}: {canonical.name}, unique doc_id="
            f"{result['output_unique_doc_ids']}, backup={len(moved)}",
            flush=True,
        )
        return result

    def run(self) -> dict[str, Any]:
        if not self.output_dir.is_dir():
            raise FileNotFoundError(self.output_dir)
        for year in range(self.start_year, self.end_year + 1):
            self.results[str(year)] = self.process_year(year)
            payload = {
                "version": 1,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "range": [self.start_year, self.end_year],
                "dry_run": self.dry_run,
                "output_dir": str(self.output_dir),
                "backup_dir": str(self.backup_dir),
                "years": self.results,
            }
            if not self.dry_run:
                atomic_json(self.report_path, payload)
        return self.results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Объединить PDF НРАТ в один проверенный ZIP на год."
    )
    parser.add_argument("--output-dir", type=Path, default=default_output_dir())
    parser.add_argument("--start-year", type=int, default=1991)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1991 <= args.start_year <= args.end_year <= 2025:
        parser.error("Диапазон должен находиться внутри 1991–2025")
    return args


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    try:
        Consolidator(
            args.output_dir,
            args.start_year,
            args.end_year,
            dry_run=args.dry_run,
        ).run()
        return 0
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"❌ Консолидация остановлена: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
