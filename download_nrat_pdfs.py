"""Последовательная загрузка и безопасное восстановление PDF НРАТ.

Скрипт рассчитан на долгий, возобновляемый запуск. Он обходит выдачу по одному
дню, сохраняет PDF во временную папку, а после полного успешного года создаёт
ZIP и только затем удаляет временные файлы этого года. Режим ``--repair``
индексирует старые ZIP по ``doc_id`` и создаёт отдельный patch-ZIP только с
отсутствующими PDF, не изменяя исходные архивы.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


BASE_SEARCH_URL = "https://nrat.ukrintei.ua/searchdb/"
PDF_BASE_URL = "https://dir.ukrintei.ua/view/ok/"


def find_default_output_dir() -> Path:
    """Найти существующую папку ``nrat pdfs`` рядом с репозиторием."""
    cwd = Path.cwd().resolve()
    candidates = (cwd / "nrat pdfs", cwd.parent / "nrat pdfs")
    return next((path for path in candidates if path.is_dir()), candidates[1])


@dataclass
class Settings:
    start_year: int = 1991
    end_year: int = 2025
    newest_first: bool = True
    output_dir: Path = field(default_factory=find_default_output_dir)
    days_per_chunk: int = 1
    delay_between_pages: int = 3
    delay_between_files: int = 1
    delay_between_days: int = 5
    retry_count: int = 3
    max_pages_per_day: int = 100
    skip_existing_zips: bool = True
    delete_staging_after_zip: bool = True

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir).expanduser().resolve()
        if not 1991 <= self.start_year <= self.end_year <= 2025:
            raise ValueError("Диапазон годов должен находиться внутри 1991–2025")
        if self.days_per_chunk != 1:
            raise ValueError("Для лимита выдачи НРАТ days_per_chunk должен быть равен 1")
        minimums = {
            "delay_between_pages": 3,
            "delay_between_files": 1,
            "delay_between_days": 5,
        }
        for name, minimum in minimums.items():
            if getattr(self, name) < minimum:
                raise ValueError(f"{name} нельзя устанавливать меньше {minimum} сек.")


class NratDownloader:
    """Возобновляемый загрузчик годовых архивов НРАТ."""

    def __init__(self, settings: Settings | None = None) -> None:
        configure_console_encoding()
        self.settings = settings or Settings()
        self.output_dir = self.settings.output_dir
        self.work_dir = self.output_dir / ".nrat_work"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "uk,en;q=0.9",
            }
        )
        self.base_params = {
            "typeSearch2": "ok",
            "typeCategory[]": "0",
            "lcSource": "",
            "authorSearch": "",
            "specialnistSearch[]": "0",
            "temaSearch2": "",
            "textSearch": "",
            "registrationNumberSearch": "",
            "firm_id": "0",
            "sortOrder": "registration_date",
            "sortDir": "desc",
            "tab": "big",
        }

    def describe(self) -> None:
        if self.settings.newest_first:
            order = f"от {self.settings.end_year} к {self.settings.start_year}"
        else:
            order = f"от {self.settings.start_year} к {self.settings.end_year}"
        print(f"Диапазон: {self.settings.start_year}–{self.settings.end_year} ({order})")
        print(f"ZIP и служебные файлы: {self.output_dir}")
        print(f"Временные PDF: {self.work_dir}")
        existing = [year for year in self.year_order() if self.usable_archive_for_year(year)]
        print(f"Уже есть корректные ZIP для {len(existing)} годов: {existing or 'нет'}")

    def year_order(self) -> list[int]:
        years = list(range(self.settings.start_year, self.settings.end_year + 1))
        return list(reversed(years)) if self.settings.newest_first else years

    def year_dir(self, year: int) -> Path:
        return self.work_dir / str(year)

    def day_folder(self, year: int, date_from: str) -> Path:
        return self.year_dir(year) / date_from[:7] / date_from

    def checkpoint_path(self, year: int) -> Path:
        return self.output_dir / f"_progress_check_{year}.json"

    def error_log_path(self, year: int) -> Path:
        return self.output_dir / f"_errors_{year}.txt"

    def archives_for_year(self, year: int) -> list[Path]:
        pattern = re.compile(rf"^{year}(?:$|[-_.])", re.IGNORECASE)
        return sorted(
            path
            for path in self.output_dir.glob(f"{year}*.zip")
            if (
                path.is_file()
                and pattern.match(path.stem)
                and "-repair" not in path.stem.lower()
            )
        )

    @staticmethod
    def archive_is_usable(path: Path) -> bool:
        try:
            if not zipfile.is_zipfile(path):
                return False
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                return any(
                    name.lower().endswith(".pdf") or name.endswith("_manifest.json")
                    for name in names
                )
        except (OSError, zipfile.BadZipFile):
            return False

    def usable_archive_for_year(self, year: int) -> Path | None:
        for path in self.archives_for_year(year):
            if self.archive_is_usable(path):
                return path
            print(f"⚠️ Архив повреждён или пуст и будет проигнорирован: {path.name}")
        return None

    def load_checkpoint(self, year: int) -> set[str]:
        path = self.checkpoint_path(year)
        if not path.exists():
            return set()
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return set(data.get("completed_dates", []))
        except (OSError, ValueError, TypeError) as exc:
            print(f"⚠️ Не удалось прочитать {path.name}: {exc}")
            return set()

    def save_checkpoint(self, year: int, completed: set[str]) -> None:
        path = self.checkpoint_path(year)
        temporary = path.with_suffix(path.suffix + ".tmp")
        payload = {"year": year, "completed_dates": sorted(completed)}
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)

    def log_error(
        self,
        year: int,
        date_str: str,
        registration: str,
        doc_id: str,
        status: str,
        reason: str,
        url: str,
    ) -> None:
        path = self.error_log_path(year)
        new_file = not path.exists()
        clean_reason = str(reason).replace("\r", " ").replace("\n", " ")
        with path.open("a", encoding="utf-8") as handle:
            if new_file:
                handle.write(
                    "дата_час | день | статус | рег_номер | doc_id | причина | ссылка\n"
                )
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            handle.write(
                f"{timestamp} | {date_str} | {status} | {registration} | "
                f"{doc_id} | {clean_reason} | {url}\n"
            )

    def refresh_token(self) -> None:
        """Добавить токен только если сайт снова начнёт его отдавать."""
        try:
            response = self.session.get(BASE_SEARCH_URL, timeout=(30, 60))
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")
            token = soup.find("input", attrs={"name": "_token"})
            if token and token.get("value"):
                self.base_params["_token"] = token["value"]
                print("🔑 Получен актуальный _token")
            else:
                self.base_params.pop("_token", None)
                print("ℹ️ Сайт работает без поля _token")
        except requests.RequestException as exc:
            self.base_params.pop("_token", None)
            print(f"⚠️ Стартовая страница недоступна: {exc}")

    @staticmethod
    def generate_date_ranges(
        start_date_str: str, end_date_str: str, days_per_chunk: int = 1
    ) -> list[tuple[str, str]]:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
        ranges: list[tuple[str, str]] = []
        current = start_date
        while current <= end_date:
            current_end = min(
                current + timedelta(days=days_per_chunk - 1), end_date
            )
            ranges.append(
                (current.strftime("%Y-%m-%d"), current_end.strftime("%Y-%m-%d"))
            )
            current = current_end + timedelta(days=1)
        return ranges

    def build_search_url(self, date_from: str, date_to: str, page: int = 1) -> str:
        params = self.base_params.copy()
        params["dateFromSearch"] = date_from
        params["dateToSearch"] = date_to
        params["pa"] = str(page)
        return f"{BASE_SEARCH_URL}?{urlencode(params, doseq=True)}"

    @staticmethod
    def extract_total_results(soup: BeautifulSoup) -> int | None:
        text = soup.get_text(" ", strip=True)
        patterns = (
            r"Знайдено\s+документів:\s*([\d\s.,]+)",
            r"([\d\s.,]+)\s+documents?\s+found",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                digits = re.sub(r"\D", "", match.group(1))
                return int(digits) if digits else 0
        return None

    @staticmethod
    def response_matches_dates(
        soup: BeautifulSoup, date_from: str, date_to: str
    ) -> bool:
        from_input = soup.find("input", attrs={"name": "dateFromSearch"})
        to_input = soup.find("input", attrs={"name": "dateToSearch"})
        return bool(
            from_input
            and to_input
            and from_input.get("value") == date_from
            and to_input.get("value") == date_to
        )

    @staticmethod
    def parse_results(soup: BeautifulSoup) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for card in soup.find_all("div", class_="my-card-body"):
            links = card.find_all("a", href=True)
            detail_link = next(
                (
                    link
                    for link in links
                    if re.search(r"(?:nddkr|dir)\.ukrintei\.ua", link["href"])
                ),
                None,
            )
            if detail_link is None:
                continue

            detail_url = urljoin(BASE_SEARCH_URL, detail_link["href"])
            doc_id = Path(urlparse(detail_url).path.rstrip("/")).name
            if not doc_id:
                continue

            card_text = card.get_text(" ", strip=True)
            reg_match = re.search(r"\b\d{4}U\d{6}\b", card_text, flags=re.IGNORECASE)
            registration = (
                reg_match.group(0) if reg_match else detail_link.get_text(strip=True)
            )
            results.append(
                {
                    "registration": registration or "unknown",
                    "detail_url": detail_url,
                    "pdf_url": urljoin(PDF_BASE_URL, doc_id),
                    "title": card_text[:200],
                    "doc_id": doc_id,
                }
            )
        return results

    @staticmethod
    def _retry_delay(attempt: int, response: requests.Response | None = None) -> int:
        delay = 15 * (attempt + 1)
        if response is not None and response.headers.get("Retry-After", "").isdigit():
            delay = max(delay, int(response.headers["Retry-After"]))
        return min(delay, 300)

    def get_search_results_page(
        self, date_from: str, date_to: str, page: int
    ) -> tuple[list[dict[str, str]] | None, BeautifulSoup | None]:
        url = self.build_search_url(date_from, date_to, page)
        for attempt in range(self.settings.retry_count):
            response: requests.Response | None = None
            try:
                response = self.session.get(url, timeout=(30, 90))
                response.raise_for_status()
                soup = BeautifulSoup(response.content, "html.parser")
                results = self.parse_results(soup)
                total = self.extract_total_results(soup)

                # Общая/аварийная страница без результатов не должна считаться
                # корректным пустым днём: иначе чекпоинт навсегда скроет пропуск.
                if not results and total is None and not self.response_matches_dates(
                    soup, date_from, date_to
                ):
                    raise RuntimeError(
                        "сайт вернул страницу без подтверждённой поисковой выдачи"
                    )
                return results, soup
            except (requests.RequestException, RuntimeError) as exc:
                print(
                    f"    ⚠️ выдача, попытка {attempt + 1}/"
                    f"{self.settings.retry_count}: {exc}"
                )
                if attempt < self.settings.retry_count - 1:
                    delay = self._retry_delay(attempt, response)
                    print(f"       повтор через {delay} сек.")
                    time.sleep(delay)
        return None, None

    @staticmethod
    def _safe_filename(registration: str, doc_id: str) -> str:
        safe_registration = re.sub(r"[^\w\-_.]", "_", registration)
        return re.sub(r"_+", "_", f"{safe_registration}_{doc_id}.pdf")

    @staticmethod
    def _valid_pdf(path: Path) -> bool:
        try:
            if path.stat().st_size <= 4:
                return False
            with path.open("rb") as handle:
                return handle.read(4) == b"%PDF"
        except OSError:
            return False

    def download_pdf(
        self,
        pdf_url: str,
        registration: str,
        doc_id: str,
        folder: Path,
    ) -> tuple[str, Path | None, str | None]:
        folder.mkdir(parents=True, exist_ok=True)
        filepath = folder / self._safe_filename(registration, doc_id)
        partial = filepath.with_suffix(filepath.suffix + ".part")

        if filepath.exists() and self._valid_pdf(filepath):
            return "skip", filepath, None
        if filepath.exists():
            filepath.unlink()

        last_error = "неизвестная ошибка"
        for attempt in range(self.settings.retry_count):
            response: requests.Response | None = None
            try:
                partial.unlink(missing_ok=True)
                response = self.session.get(pdf_url, stream=True, timeout=(30, 120))
                if response.status_code in (404, 410):
                    return "notpdf", None, f"на сайте нет PDF ({response.status_code})"
                if response.status_code == 500:
                    return (
                        "server500",
                        None,
                        f"500 Server Error: Internal Server Error for url: {pdf_url}",
                    )
                response.raise_for_status()
                with partial.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            handle.write(chunk)

                if partial.stat().st_size == 0:
                    partial.unlink(missing_ok=True)
                    last_error = "сервер вернул пустой файл"
                    if attempt < self.settings.retry_count - 1:
                        delay = self._retry_delay(attempt, response)
                        print(f"    ⚠️ {last_error}; повтор через {delay} сек.")
                        time.sleep(delay)
                        continue
                    return "empty", None, last_error
                if not self._valid_pdf(partial):
                    partial.unlink(missing_ok=True)
                    last_error = "HTTP 200, но ответ сервера не является PDF"
                    if attempt < self.settings.retry_count - 1:
                        delay = self._retry_delay(attempt, response)
                        print(f"    ⚠️ {last_error}; повтор через {delay} сек.")
                        time.sleep(delay)
                        continue
                    return "fail", None, last_error

                os.replace(partial, filepath)
                return "ok", filepath, None
            except (OSError, requests.RequestException) as exc:
                last_error = str(exc)
                partial.unlink(missing_ok=True)
                print(
                    f"    ❌ PDF, попытка {attempt + 1}/"
                    f"{self.settings.retry_count}: {exc}"
                )
                if attempt < self.settings.retry_count - 1:
                    delay = self._retry_delay(attempt, response)
                    print(f"       повтор через {delay} сек.")
                    time.sleep(delay)
        return "fail", None, last_error

    def scrape_day(
        self, year: int, date_from: str, date_to: str
    ) -> tuple[dict[str, int], bool]:
        folder = self.day_folder(year, date_from)
        folder.mkdir(parents=True, exist_ok=True)
        page = 1
        page_size: int | None = None
        total_pages: int | None = None
        seen_ids: set[str] = set()
        stats = {"ok": 0, "skip": 0, "fail": 0, "notpdf": 0, "empty": 0}

        while True:
            url = self.build_search_url(date_from, date_to, page)
            results, soup = self.get_search_results_page(date_from, date_to, page)
            if results is None or soup is None:
                reason = "страница поисковой выдачи не загрузилась или не прошла проверку"
                self.log_error(year, date_from, "-", "-", "page_fail", reason, url)
                return stats, False

            if page == 1:
                total_results = self.extract_total_results(soup)
                page_size = len(results) if results else 10
                if total_results is not None:
                    total_pages = max(1, (total_results + page_size - 1) // page_size)
                    print(f"    найдено {total_results}, страниц: {total_pages}")
                if total_results is not None and total_results >= 1000:
                    print("    ⚠️ За день найдено 1000+ результатов; выдача сайта ограничена")

            if not results:
                if page == 1:
                    print("    результатов нет")
                break

            new_results = [item for item in results if item["doc_id"] not in seen_ids]
            if page > 1 and not new_results:
                if total_pages is not None and page <= total_pages:
                    reason = "пагинация повторила предыдущую страницу"
                    self.log_error(year, date_from, "-", "-", "page_repeat", reason, url)
                    return stats, False
                break
            seen_ids.update(item["doc_id"] for item in new_results)

            for index, item in enumerate(new_results, 1):
                status, _, reason = self.download_pdf(
                    item["pdf_url"], item["registration"], item["doc_id"], folder
                )
                stats[status if status in stats else "fail"] += 1
                if status not in ("ok", "skip"):
                    self.log_error(
                        year,
                        date_from,
                        item["registration"],
                        item["doc_id"],
                        status,
                        reason or "неизвестная ошибка",
                        item["pdf_url"],
                    )
                if index < len(new_results):
                    time.sleep(self.settings.delay_between_files)

            if total_pages is not None:
                go_next = page < total_pages
            else:
                go_next = bool(page_size and len(results) >= page_size)
            if not go_next:
                break
            if page >= self.settings.max_pages_per_day:
                reason = f"достигнут лимит {self.settings.max_pages_per_day} страниц"
                self.log_error(year, date_from, "-", "-", "page_limit", reason, url)
                return stats, False

            page += 1
            time.sleep(self.settings.delay_between_pages)

        return stats, True

    @staticmethod
    def _sum_stats(target: dict[str, int], source: dict[str, int]) -> None:
        for key in target:
            target[key] += source[key]

    def _write_manifest(self, year: int, pdf_count: int) -> Path:
        manifest = self.year_dir(year) / "_manifest.json"
        payload = {
            "year": year,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "pdf_count": pdf_count,
            "source": BASE_SEARCH_URL,
            "date_range": [f"{year}-01-01", f"{year}-12-31"],
        }
        with manifest.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return manifest

    def finalize_year_archive(self, year: int) -> Path:
        year_dir = self.year_dir(year)
        pdf_files = sorted(year_dir.rglob("*.pdf"))
        self._write_manifest(year, len(pdf_files))

        final_path = self.output_dir / f"{year}.zip"
        temporary_path = self.output_dir / f".{year}.tmp.zip"
        temporary_path.unlink(missing_ok=True)
        print(f"📦 Создаётся {final_path.name} ({len(pdf_files)} PDF)…")

        with zipfile.ZipFile(
            temporary_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True
        ) as archive:
            for path in sorted(item for item in year_dir.rglob("*") if item.is_file()):
                archive.write(path, path.relative_to(year_dir.parent))

        with zipfile.ZipFile(temporary_path) as archive:
            archived_pdfs = sum(
                1 for name in archive.namelist() if name.lower().endswith(".pdf")
            )
            manifest_name = f"{year}/_manifest.json"
            if archived_pdfs != len(pdf_files) or manifest_name not in archive.namelist():
                raise RuntimeError(
                    f"Проверка ZIP не пройдена: ожидалось {len(pdf_files)} PDF, "
                    f"найдено {archived_pdfs}"
                )

        os.replace(temporary_path, final_path)
        print(f"✅ Архив готов: {final_path}")
        if self.settings.delete_staging_after_zip:
            shutil.rmtree(year_dir)
            print(f"🧹 Временная папка года удалена: {year_dir}")
        return final_path

    def run_year(self, year: int) -> tuple[bool, dict[str, int]]:
        completed = self.load_checkpoint(year)
        date_ranges = self.generate_date_ranges(
            f"{year}-01-01", f"{year}-12-31", self.settings.days_per_chunk
        )
        all_dates = {date_from for date_from, _ in date_ranges}
        completed.intersection_update(all_dates)
        grand = {"ok": 0, "skip": 0, "fail": 0, "notpdf": 0, "empty": 0}

        print("\n" + "=" * 72)
        print(f"ГОД {year}: уже завершено {len(completed)} из {len(all_dates)} дней")
        print("=" * 72)
        self.refresh_token()

        for index, (date_from, date_to) in enumerate(date_ranges, 1):
            if date_from in completed:
                continue

            print(f"\n📅 {date_from} ({index}/{len(date_ranges)})")
            stats, page_ok = self.scrape_day(year, date_from, date_to)
            self._sum_stats(grand, stats)
            print(
                f"    итог: новых {stats['ok']}, уже было {stats['skip']}, "
                f"без PDF {stats['notpdf']}, ошибок {stats['fail']}, "
                f"пустых {stats['empty']}"
            )

            day_complete = page_ok and stats["fail"] == 0 and stats["empty"] == 0
            if day_complete:
                completed.add(date_from)
                self.save_checkpoint(year, completed)
            time.sleep(self.settings.delay_between_days)

        missing_dates = sorted(all_dates - completed)
        if missing_dates:
            print(
                f"\n⚠️ Год {year} пока не упакован: незавершённых дней "
                f"{len(missing_dates)}. Повторный запуск обработает только их."
            )
            print(f"Первые незавершённые даты: {missing_dates[:20]}")
            print(f"Лог: {self.error_log_path(year)}")
            return False, grand

        self.finalize_year_archive(year)
        return True, grand

    def run_all(self) -> dict[int, str]:
        """Обработать 1991–2025 последовательно, пропуская готовые ZIP."""
        self.describe()
        statuses: dict[int, str] = {}
        for year in self.year_order():
            existing = self.usable_archive_for_year(year)
            if existing and self.settings.skip_existing_zips:
                print(f"⏭ {year}: уже есть {existing.name}")
                statuses[year] = "пропущен: ZIP уже существует"
                continue

            complete, _ = self.run_year(year)
            if complete:
                statuses[year] = "готов"
                print(f"➡️ {year} завершён, автоматически переходим к следующему году")
            else:
                statuses[year] = "нужен повторный запуск"
                print("⏸ Переход к следующему году остановлен до завершения текущего")
                break
        return statuses


class NratRepairer(NratDownloader):
    """Найти отсутствующие ``doc_id`` и создать отдельные patch-ZIP.

    Старые ZIP никогда не перезаписываются. Собственный чекпоинт режима
    восстановления хранит не только завершённые даты, но и точный список
    ``doc_id`` и подтверждённых ответов без PDF для каждого дня. Поэтому старые
    ложные ``_progress_check_*.json`` здесь вообще не читаются.
    """

    DOC_ID_PATTERN = re.compile(r"_([0-9a-f]{32})\.pdf$", re.IGNORECASE)
    DATE_PATTERN = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self.legacy_work_dir = self.work_dir
        self.repair_work_dir = self.output_dir / ".nrat_repair_work"
        self.repair_work_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir = self.repair_work_dir
        self._loose_index_cache: dict[int, dict[str, Path]] = {}

    def archives_for_year(self, year: int) -> list[Path]:
        """В repair-режиме учитывать как исходные, так и прежние patch-ZIP."""
        pattern = re.compile(rf"^{year}(?:$|[-_.])", re.IGNORECASE)
        return sorted(
            path
            for path in self.output_dir.glob(f"{year}*.zip")
            if path.is_file() and pattern.match(path.stem)
        )

    def checkpoint_path(self, year: int) -> Path:
        return self.output_dir / f"_repair_progress_{year}.json"

    def error_log_path(self, year: int) -> Path:
        return self.output_dir / f"_repair_errors_{year}.txt"

    def report_path(self, year: int) -> Path:
        return self.output_dir / f"_repair_report_{year}.json"

    def known_server500_ids(self, year: int) -> set[str]:
        """Прочитать уже подтверждённые HTTP 500 из исторического лога."""
        path = self.error_log_path(year)
        result: set[str] = set()
        if not path.exists():
            return result
        try:
            with path.open("r", encoding="utf-8") as handle:
                next(handle, None)
                for line in handle:
                    parts = [part.strip() for part in line.split("|")]
                    if len(parts) < 7:
                        continue
                    doc_id = parts[4].lower()
                    reason = parts[5]
                    if (
                        doc_id != "-"
                        and re.fullmatch(r"[0-9a-f]{32}", doc_id)
                        and re.search(
                            r"(?:\b500\s+Server\s+Error\b|Internal\s+Server\s+Error)",
                            reason,
                            flags=re.IGNORECASE,
                        )
                    ):
                        result.add(doc_id)
        except OSError as exc:
            print(f"⚠️ Не удалось прочитать HTTP 500 из {path.name}: {exc}")
        return result

    def legacy_terminal_server500_days(
        self, year: int
    ) -> dict[str, dict[str, set[str]]]:
        """Найти все старые даты, на которых был хотя бы один HTTP 500.

        До появления статуса ``server500`` такие ответы записывались как
        ``fail``, поэтому день не попадал в чекпоинт. По прямому решению
        пользователя вся такая дата становится терминальной; дополнительные
        ошибки не удаляются из лога и переносятся в запись чекпоинта.
        """
        path = self.error_log_path(year)
        days: dict[str, dict[str, set[str]]] = {}
        if not path.exists():
            return days
        try:
            with path.open("r", encoding="utf-8") as handle:
                next(handle, None)
                for line in handle:
                    parts = [part.strip() for part in line.split("|")]
                    if len(parts) < 7:
                        continue
                    date, status, doc_id, reason = (
                        parts[1],
                        parts[2],
                        parts[4].lower(),
                        parts[5],
                    )
                    if not date.startswith(f"{year}-"):
                        continue
                    valid_doc_id = bool(re.fullmatch(r"[0-9a-f]{32}", doc_id))
                    is_server500 = bool(
                        valid_doc_id
                        and re.search(
                            r"(?:\b500\s+Server\s+Error\b|Internal\s+Server\s+Error)",
                            reason,
                            flags=re.IGNORECASE,
                        )
                    )
                    day = days.setdefault(
                        date,
                        {
                            "server500_doc_ids": set(),
                            "no_pdf_doc_ids": set(),
                            "additional_statuses": set(),
                        },
                    )
                    if is_server500:
                        day["server500_doc_ids"].add(doc_id)
                    elif status == "notpdf" and valid_doc_id:
                        day["no_pdf_doc_ids"].add(doc_id)
                    else:
                        day["additional_statuses"].add(status)
        except OSError as exc:
            print(f"⚠️ Не удалось прочитать старые даты HTTP 500: {exc}")
            return {}
        return {
            date: values
            for date, values in days.items()
            if values["server500_doc_ids"]
        }

    def existing_pdf_ids_by_date(self, year: int) -> dict[str, set[str]]:
        """Сопоставить уже сохранённые PDF с датами из путей внутри коллекции."""
        result: dict[str, set[str]] = {}

        def add(name: str) -> None:
            doc_id = self.doc_id_from_name(name)
            date_match = self.DATE_PATTERN.search(name)
            if doc_id and date_match and date_match.group(1).startswith(f"{year}-"):
                result.setdefault(date_match.group(1), set()).add(doc_id)

        for archive_path in self.archives_for_year(year):
            try:
                if not zipfile.is_zipfile(archive_path):
                    continue
                with zipfile.ZipFile(archive_path) as archive:
                    for name in archive.namelist():
                        if name.lower().endswith(".pdf"):
                            add(name)
            except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
                print(f"⚠️ Не удалось сопоставить даты внутри {archive_path.name}: {exc}")

        for root in (*self._staging_roots(year), self.output_dir / str(year)):
            if not root.is_dir():
                continue
            for path in root.rglob("*.pdf"):
                if self._valid_pdf(path):
                    add(str(path))
        return result

    def migrate_legacy_server500_days(
        self, year: int, state: dict[str, Any]
    ) -> list[str]:
        """Перенести чистые старые HTTP-500-даты в чекпоинт без запросов к сайту."""
        candidates = self.legacy_terminal_server500_days(year)
        missing_dates = [date for date in candidates if date not in state["days"]]
        if not missing_dates:
            return []
        existing_by_date = self.existing_pdf_ids_by_date(year)
        migrated: list[str] = []
        for date in sorted(missing_dates):
            values = candidates[date]
            server500_ids = values["server500_doc_ids"]
            no_pdf_ids = values["no_pdf_doc_ids"]
            doc_ids = existing_by_date.get(date, set()) | server500_ids | no_pdf_ids
            state["days"][date] = {
                "date_to": date,
                "site_total": None,
                "doc_ids": sorted(doc_ids),
                "no_pdf_doc_ids": sorted(no_pdf_ids),
                "server500_doc_ids": sorted(server500_ids),
                "accepted_error": "legacy_server500_log",
                "additional_logged_statuses": sorted(
                    values.get("additional_statuses", set())
                ),
                "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            migrated.append(date)
        if migrated:
            self.save_repair_state(year, state)
        return migrated

    @classmethod
    def doc_id_from_name(cls, name: str) -> str | None:
        match = cls.DOC_ID_PATTERN.search(Path(name).name)
        return match.group(1).lower() if match else None

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)

    def load_repair_state(self, year: int) -> dict[str, Any]:
        path = self.checkpoint_path(year)
        blank: dict[str, Any] = {"version": 1, "year": year, "days": {}}
        if not path.exists():
            return blank
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if payload.get("year") != year or not isinstance(payload.get("days"), dict):
                raise ValueError("неверная структура чекпоинта")
            return payload
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"⚠️ {path.name} не принят ({exc}); аудит года начнётся заново")
            return blank

    def save_repair_state(self, year: int, state: dict[str, Any]) -> None:
        state["version"] = 1
        state["year"] = year
        state["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        self._atomic_write_json(self.checkpoint_path(year), state)

    def archive_pdf_ids(self, year: int) -> tuple[set[str], list[str]]:
        doc_ids: set[str] = set()
        accepted_archives: list[str] = []
        for path in self.archives_for_year(year):
            try:
                if not zipfile.is_zipfile(path):
                    print(f"⚠️ Не является ZIP и не индексируется: {path.name}")
                    continue
                with zipfile.ZipFile(path) as archive:
                    for name in archive.namelist():
                        if name.lower().endswith(".pdf"):
                            doc_id = self.doc_id_from_name(name)
                            if doc_id:
                                doc_ids.add(doc_id)
                accepted_archives.append(path.name)
            except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
                print(f"⚠️ Не удалось прочитать {path.name}: {exc}")
        return doc_ids, accepted_archives

    def _staging_roots(self, year: int) -> list[Path]:
        return [self.repair_work_dir / str(year), self.legacy_work_dir / str(year)]

    def staged_pdf_map(self, year: int) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for root in self._staging_roots(year):
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.pdf")):
                doc_id = self.doc_id_from_name(path.name)
                if doc_id and doc_id not in result and self._valid_pdf(path):
                    result[doc_id] = path
        return result

    def loose_existing_pdf_map(self, year: int) -> dict[str, Path]:
        """Индексировать старую папку ``OUTPUT_DIR/<год>`` на Google Drive."""
        if year in self._loose_index_cache:
            return dict(self._loose_index_cache[year])
        root = self.output_dir / str(year)
        result: dict[str, Path] = {}
        if not root.is_dir():
            self._loose_index_cache[year] = result
            return result
        for path in sorted(root.rglob("*.pdf")):
            doc_id = self.doc_id_from_name(path.name)
            if doc_id and doc_id not in result and self._valid_pdf(path):
                result[doc_id] = path
        self._loose_index_cache[year] = result
        return dict(result)

    def verified_report_exists(self, year: int) -> bool:
        path = self.report_path(year)
        try:
            with path.open("r", encoding="utf-8") as handle:
                report = json.load(handle)
            archives = report.get("archives_used", [])
            loose_folder_ok = (
                not report.get("loose_existing_pdf_count")
                or (self.output_dir / str(year)).is_dir()
            )
            return bool(
                report.get("complete") is True
                and all((self.output_dir / name).is_file() for name in archives)
                and loose_folder_ok
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def repair_day(
        self,
        year: int,
        date_from: str,
        date_to: str,
        available_ids: set[str],
        accepted_server500_ids: set[str],
    ) -> tuple[dict[str, Any], dict[str, int], bool]:
        folder = self.day_folder(year, date_from)
        folder.mkdir(parents=True, exist_ok=True)
        page = 1
        page_size: int | None = None
        total_pages: int | None = None
        total_results: int | None = None
        seen_ids: set[str] = set()
        no_pdf_ids: set[str] = set()
        server500_ids: set[str] = set()
        stats = {
            "site": 0,
            "already": 0,
            "ok": 0,
            "notpdf": 0,
            "server500": 0,
            "fail": 0,
            "empty": 0,
        }
        pages_ok = True

        while True:
            url = self.build_search_url(date_from, date_to, page)
            results, soup = self.get_search_results_page(date_from, date_to, page)
            if results is None or soup is None:
                reason = "страница выдачи не загрузилась или не прошла проверку"
                self.log_error(year, date_from, "-", "-", "page_fail", reason, url)
                pages_ok = False
                break

            if page == 1:
                total_results = self.extract_total_results(soup)
                if total_results is None:
                    reason = "на странице отсутствует подтверждённое общее количество"
                    self.log_error(
                        year, date_from, "-", "-", "count_missing", reason, url
                    )
                    pages_ok = False
                    break
                page_size = len(results) if results else 10
                total_pages = max(1, (total_results + page_size - 1) // page_size)
                print(f"    сайт: {total_results}, страниц: {total_pages}")

            if not results:
                break

            new_results = [item for item in results if item["doc_id"] not in seen_ids]
            if page > 1 and not new_results:
                reason = "пагинация повторила предыдущую страницу"
                self.log_error(
                    year, date_from, "-", "-", "page_repeat", reason, url
                )
                pages_ok = False
                break

            seen_ids.update(item["doc_id"] for item in new_results)
            accepted_500_items = [
                item
                for item in new_results
                if item["doc_id"] not in available_ids
                and item["doc_id"] in accepted_server500_ids
            ]
            server500_ids.update(item["doc_id"] for item in accepted_500_items)
            stats["server500"] += len(accepted_500_items)
            missing_items = [
                item
                for item in new_results
                if item["doc_id"] not in available_ids
                and item["doc_id"] not in accepted_server500_ids
            ]
            stats["already"] += (
                len(new_results) - len(missing_items) - len(accepted_500_items)
            )

            for index, item in enumerate(missing_items, 1):
                status, _, reason = self.download_pdf(
                    item["pdf_url"], item["registration"], item["doc_id"], folder
                )
                if status in ("ok", "skip"):
                    stats["ok"] += int(status == "ok")
                    stats["already"] += int(status == "skip")
                    available_ids.add(item["doc_id"])
                elif status == "notpdf":
                    stats["notpdf"] += 1
                    no_pdf_ids.add(item["doc_id"])
                    self.log_error(
                        year,
                        date_from,
                        item["registration"],
                        item["doc_id"],
                        status,
                        reason or "на сайте нет PDF",
                        item["pdf_url"],
                    )
                elif status == "server500":
                    stats["server500"] += 1
                    server500_ids.add(item["doc_id"])
                    accepted_server500_ids.add(item["doc_id"])
                    self.log_error(
                        year,
                        date_from,
                        item["registration"],
                        item["doc_id"],
                        status,
                        reason or "HTTP 500 на сервере PDF",
                        item["pdf_url"],
                    )
                else:
                    normalized = status if status in ("fail", "empty") else "fail"
                    stats[normalized] += 1
                    self.log_error(
                        year,
                        date_from,
                        item["registration"],
                        item["doc_id"],
                        normalized,
                        reason or "неизвестная ошибка",
                        item["pdf_url"],
                    )
                if index < len(missing_items):
                    time.sleep(self.settings.delay_between_files)

            if total_pages is not None:
                go_next = page < total_pages
            else:
                go_next = bool(page_size and len(results) >= page_size)
            if not go_next:
                break
            if page >= self.settings.max_pages_per_day:
                reason = f"достигнут лимит {self.settings.max_pages_per_day} страниц"
                self.log_error(year, date_from, "-", "-", "page_limit", reason, url)
                pages_ok = False
                break
            page += 1
            time.sleep(self.settings.delay_between_pages)

        stats["site"] = len(seen_ids)
        if total_results is None or len(seen_ids) != total_results:
            reason = (
                f"сайт объявил {total_results}, собрано уникальных doc_id "
                f"{len(seen_ids)}"
            )
            self.log_error(
                year,
                date_from,
                "-",
                "-",
                "count_mismatch",
                reason,
                self.build_search_url(date_from, date_to, 1),
            )
            pages_ok = False

        complete = pages_ok and stats["fail"] == 0 and stats["empty"] == 0
        record = {
            "date_to": date_to,
            "site_total": total_results,
            "doc_ids": sorted(seen_ids),
            "no_pdf_doc_ids": sorted(no_pdf_ids),
            "server500_doc_ids": sorted(server500_ids),
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        return record, stats, complete

    def annual_total(self, year: int) -> int | None:
        date_from = f"{year}-01-01"
        date_to = f"{year}-12-31"
        results, soup = self.get_search_results_page(date_from, date_to, 1)
        if results is None or soup is None:
            return None
        if not self.response_matches_dates(soup, date_from, date_to):
            return None
        return self.extract_total_results(soup)

    def finalize_patch_archive(
        self,
        year: int,
        site_ids: set[str],
        persisted_ids: set[str],
        report: dict[str, Any],
    ) -> Path | None:
        staged = self.staged_pdf_map(year)
        new_items = {
            doc_id: path
            for doc_id, path in staged.items()
            if doc_id in site_ids and doc_id not in persisted_ids
        }
        if not new_items:
            print("📦 Новых PDF для patch-ZIP нет; создан только отчёт проверки")
            return None

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        final_path = self.output_dir / f"{year}-repair-{stamp}.zip"
        temporary_path = self.output_dir / f".{year}-repair.tmp.zip"
        temporary_path.unlink(missing_ok=True)
        manifest_name = f"{year}/_repair_manifest.json"
        manifest = dict(report)
        manifest["patch_pdf_count"] = len(new_items)
        manifest["patch_doc_ids"] = sorted(new_items)

        print(f"📦 Создаётся {final_path.name}: {len(new_items)} новых PDF")
        used_names: set[str] = set()
        with zipfile.ZipFile(
            temporary_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True
        ) as archive:
            for doc_id, path in sorted(new_items.items()):
                date = next(
                    (
                        part
                        for part in path.parts
                        if re.fullmatch(rf"{year}-\d{{2}}-\d{{2}}", part)
                    ),
                    f"{year}-unknown",
                )
                arcname = f"{year}/{date[:7]}/{date}/{path.name}"
                if arcname in used_names:
                    arcname = f"{year}/{date[:7]}/{date}/{doc_id}_{path.name}"
                used_names.add(arcname)
                archive.write(path, arcname)
            archive.writestr(
                manifest_name,
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )

        with zipfile.ZipFile(temporary_path) as archive:
            bad_member = archive.testzip()
            patch_ids = {
                doc_id
                for name in archive.namelist()
                if name.lower().endswith(".pdf")
                for doc_id in [self.doc_id_from_name(name)]
                if doc_id
            }
            if (
                bad_member is not None
                or patch_ids != set(new_items)
                or manifest_name not in archive.namelist()
            ):
                raise RuntimeError(
                    "проверка временного patch-ZIP не пройдена; старые ZIP не изменены"
                )

        os.replace(temporary_path, final_path)
        print(f"✅ Patch-ZIP проверен и сохранён: {final_path}")

        repair_year_dir = (self.repair_work_dir / str(year)).resolve()
        if (
            self.settings.delete_staging_after_zip
            and repair_year_dir.is_dir()
            and repair_year_dir.parent == self.repair_work_dir.resolve()
        ):
            shutil.rmtree(repair_year_dir)
            print(f"🧹 Удалена только временная repair-папка: {repair_year_dir}")
        return final_path

    def run_repair_year(self, year: int, force_reaudit: bool = False) -> bool:
        if not force_reaudit and self.verified_report_exists(year):
            print(f"⏭ {year}: уже есть проверенный _repair_report_{year}.json")
            return True

        state = self.load_repair_state(year)
        dates = self.generate_date_ranges(f"{year}-01-01", f"{year}-12-31", 1)
        valid_dates = {date_from for date_from, _ in dates}
        state["days"] = {
            date: value
            for date, value in state.get("days", {}).items()
            if date in valid_dates and isinstance(value, dict)
        }

        archived_ids, archive_names = self.archive_pdf_ids(year)
        loose_existing = self.loose_existing_pdf_map(year)
        staged = self.staged_pdf_map(year)
        persisted_ids = set(archived_ids) | set(loose_existing)
        available_ids = persisted_ids | set(staged)
        accepted_server500_ids = self.known_server500_ids(year)
        print("\n" + "=" * 72)
        print(
            f"REPAIR {year}: ZIP={len(archived_ids)}, папка года={len(loose_existing)}, "
            f"staging={len(staged)}, HTTP500={len(accepted_server500_ids)}, "
            f"проверено дней={len(state['days'])}/{len(dates)}"
        )
        print("=" * 72)
        self.refresh_token()

        # Для полностью пустого года один проверенный годовой запрос сильнее и
        # намного быстрее 365 одинаковых пустых дневных запросов. Ответ
        # принимается только если сайт вернул тот же диапазон дат и явный итог 0.
        preflight_total = self.annual_total(year)
        if preflight_total == 0:
            report: dict[str, Any] = {
                "version": 1,
                "year": year,
                "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                "complete": True,
                "zero_year_fast_path": True,
                "annual_site_total": 0,
                "daily_unique_site_doc_ids": 0,
                "unique_pdf_ids_before_patch": len(available_ids),
                "unique_pdf_ids_after_patch": len(persisted_ids),
                "confirmed_no_pdf": 0,
                "confirmed_server500": 0,
                "accepted_server500_dates": [],
                "accepted_server500_dates_with_additional_errors": {},
                "unresolved_doc_ids": [],
                "unfinished_dates": [],
                "extra_pdf_ids_not_in_current_site_year": len(available_ids),
                "archives_used": archive_names,
                "loose_existing_pdf_count": len(loose_existing),
                "legacy_staging_pdf_count": len(
                    [
                        path
                        for path in staged.values()
                        if self.legacy_work_dir in path.parents
                    ]
                ),
                "patch_archive": None,
            }
            self._atomic_write_json(self.report_path(year), report)
            print(
                f"✅ {year}: НРАТ подтвердил 0 публикаций за весь год; "
                "365 дневных запросов не нужны"
            )
            return True
        if preflight_total is None:
            print("⚠️ Годовой итог не получен; полнота будет проверяться по дням")
        else:
            print(f"Годовой итог НРАТ перед проверкой: {preflight_total}")

        migrated_500_dates = self.migrate_legacy_server500_days(year, state)
        if migrated_500_dates:
            print(
                f"⏭ Старых дат только с HTTP 500 перенесено в чекпоинт без "
                f"повторных запросов: {len(migrated_500_dates)}"
            )

        for index, (date_from, date_to) in enumerate(dates, 1):
            if date_from in state["days"]:
                continue
            print(f"\n📅 {date_from} ({index}/{len(dates)})")
            record, stats, complete = self.repair_day(
                year,
                date_from,
                date_to,
                available_ids,
                accepted_server500_ids,
            )
            print(
                f"    уникальных на сайте {stats['site']}; уже было {stats['already']}; "
                f"скачано {stats['ok']}; без PDF {stats['notpdf']}; "
                f"HTTP 500 {stats['server500']}; "
                f"ошибок {stats['fail'] + stats['empty']}"
            )
            if complete:
                state["days"][date_from] = record
                self.save_repair_state(year, state)
            else:
                print("    ⚠️ День не отмечен завершённым и будет повторён")
            time.sleep(self.settings.delay_between_days)

        unfinished_dates = sorted(valid_dates - set(state["days"]))
        site_ids = {
            doc_id
            for record in state["days"].values()
            for doc_id in record.get("doc_ids", [])
        }
        no_pdf_ids = {
            doc_id
            for record in state["days"].values()
            for doc_id in record.get("no_pdf_doc_ids", [])
        }
        server500_ids = {
            doc_id
            for record in state["days"].values()
            for doc_id in record.get("server500_doc_ids", [])
        }
        server500_dates = sorted(
            date
            for date, record in state["days"].items()
            if record.get("server500_doc_ids")
        )
        server500_dates_with_additional_errors = {
            date: list(record.get("additional_logged_statuses", []))
            for date, record in sorted(state["days"].items())
            if record.get("server500_doc_ids")
            and record.get("additional_logged_statuses")
        }
        annual_total = self.annual_total(year) if not unfinished_dates else None
        archived_ids, archive_names = self.archive_pdf_ids(year)
        loose_existing = self.loose_existing_pdf_map(year)
        staged = self.staged_pdf_map(year)
        persisted_ids = set(archived_ids) | set(loose_existing)
        available_ids = persisted_ids | set(staged)
        unresolved_ids = site_ids - available_ids - no_pdf_ids - server500_ids
        count_matches = annual_total is not None and annual_total == len(site_ids)
        complete = not unfinished_dates and not unresolved_ids and count_matches

        report: dict[str, Any] = {
            "version": 1,
            "year": year,
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "complete": complete,
            "annual_site_total": annual_total,
            "daily_unique_site_doc_ids": len(site_ids),
            "unique_pdf_ids_before_patch": len(available_ids),
            "confirmed_no_pdf": len((site_ids - available_ids) & no_pdf_ids),
            "confirmed_server500": len(
                (site_ids - available_ids - no_pdf_ids) & server500_ids
            ),
            "accepted_server500_dates": server500_dates,
            "accepted_server500_dates_with_additional_errors": (
                server500_dates_with_additional_errors
            ),
            "unresolved_doc_ids": sorted(unresolved_ids),
            "unfinished_dates": unfinished_dates,
            "extra_pdf_ids_not_in_current_site_year": len(available_ids - site_ids),
            "archives_used": archive_names,
            "loose_existing_pdf_count": len(loose_existing),
            "legacy_staging_pdf_count": len(
                [
                    path
                    for path in staged.values()
                    if self.legacy_work_dir in path.parents
                ]
            ),
        }
        self._atomic_write_json(self.report_path(year), report)

        if not complete:
            print(
                f"\n⚠️ {year} не подтверждён: незавершённых дат "
                f"{len(unfinished_dates)}, нерешённых doc_id {len(unresolved_ids)}, "
                f"годовой итог сайта={annual_total}, дневных doc_id={len(site_ids)}"
            )
            print(f"Отчёт: {self.report_path(year)}")
            print(f"Ошибки: {self.error_log_path(year)}")
            return False

        patch_path = self.finalize_patch_archive(year, site_ids, persisted_ids, report)
        archived_after, archive_names_after = self.archive_pdf_ids(year)
        loose_after = self.loose_existing_pdf_map(year)
        persisted_after = set(archived_after) | set(loose_after)
        report["patch_archive"] = patch_path.name if patch_path else None
        report["archives_used"] = archive_names_after
        report["loose_existing_pdf_count"] = len(loose_after)
        report["unique_pdf_ids_after_patch"] = len(persisted_after)
        report["complete"] = not (
            site_ids - persisted_after - no_pdf_ids - server500_ids
        )
        self._atomic_write_json(self.report_path(year), report)
        print(
            f"✅ {year} проверен: сайт {annual_total}, PDF {len(site_ids & persisted_after)}, "
            f"без PDF {len((site_ids - persisted_after) & no_pdf_ids)}, "
            f"серверных HTTP 500 {len((site_ids - persisted_after - no_pdf_ids) & server500_ids)}"
        )
        return bool(report["complete"])

    def repair_log_summary(self, year: int) -> dict[str, Any]:
        path = self.error_log_path(year)
        counts: dict[str, int] = {}
        statuses_by_date: dict[str, set[str]] = {}
        if not path.exists():
            return {"counts_by_status": counts, "statuses_by_date": {}}
        try:
            with path.open("r", encoding="utf-8") as handle:
                next(handle, None)
                for line in handle:
                    parts = [part.strip() for part in line.split("|")]
                    if len(parts) < 7:
                        continue
                    date = parts[1]
                    status = parts[2]
                    counts[status] = counts.get(status, 0) + 1
                    statuses_by_date.setdefault(date, set()).add(status)
        except OSError as exc:
            return {
                "counts_by_status": counts,
                "statuses_by_date": {},
                "read_error": str(exc),
            }
        return {
            "counts_by_status": dict(sorted(counts.items())),
            "statuses_by_date": {
                date: sorted(statuses)
                for date, statuses in sorted(statuses_by_date.items())
            },
        }

    def write_repair_summary(self, statuses: dict[int, str]) -> tuple[Path, Path]:
        stamp = datetime.now(timezone.utc).isoformat()
        years: dict[str, Any] = {}
        text_lines = [
            "год | незавершённая дата/doc_id | статусы из лога | отчёт | лог",
        ]

        for year, status in statuses.items():
            report_path = self.report_path(year)
            report: dict[str, Any] = {}
            try:
                with report_path.open("r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    report = loaded
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass

            log_summary = self.repair_log_summary(year)
            unfinished_dates = list(report.get("unfinished_dates", []))
            unresolved_ids = list(report.get("unresolved_doc_ids", []))
            statuses_by_date = log_summary.get("statuses_by_date", {})
            years[str(year)] = {
                "status": status,
                "complete": report.get("complete", status != "нужен повторный запуск"),
                "annual_site_total": report.get("annual_site_total"),
                "daily_unique_site_doc_ids": report.get("daily_unique_site_doc_ids"),
                "confirmed_no_pdf": report.get("confirmed_no_pdf", 0),
                "confirmed_server500": report.get("confirmed_server500", 0),
                "accepted_server500_dates": report.get(
                    "accepted_server500_dates", []
                ),
                "accepted_server500_dates_with_additional_errors": report.get(
                    "accepted_server500_dates_with_additional_errors", {}
                ),
                "unfinished_dates": unfinished_dates,
                "unresolved_doc_ids": unresolved_ids,
                "logged_errors_by_status": log_summary.get("counts_by_status", {}),
                "logged_statuses_by_date": statuses_by_date,
                "report_file": report_path.name,
                "error_log_file": self.error_log_path(year).name,
            }

            for date in unfinished_dates:
                text_lines.append(
                    f"{year} | {date} | {','.join(statuses_by_date.get(date, [])) or '-'} "
                    f"| {report_path.name} | {self.error_log_path(year).name}"
                )
            for doc_id in unresolved_ids:
                text_lines.append(
                    f"{year} | doc_id={doc_id} | unresolved "
                    f"| {report_path.name} | {self.error_log_path(year).name}"
                )

        payload = {
            "version": 1,
            "generated_at_utc": stamp,
            "range": [self.settings.start_year, self.settings.end_year],
            "years": years,
        }
        stem = f"{self.settings.start_year}_{self.settings.end_year}"
        json_path = self.output_dir / f"_repair_summary_{stem}.json"
        text_path = self.output_dir / f"_repair_incomplete_dates_{stem}.txt"
        self._atomic_write_json(json_path, payload)
        temporary = text_path.with_suffix(text_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write("\n".join(text_lines) + "\n")
        os.replace(temporary, text_path)
        return json_path, text_path

    def run_repair_all(self, force_reaudit: bool = False) -> dict[int, str]:
        print(f"Режим: безопасное восстановление; каталог: {self.output_dir}")
        statuses: dict[int, str] = {}
        for year in self.year_order():
            complete = self.run_repair_year(year, force_reaudit=force_reaudit)
            if complete:
                statuses[year] = "проверен и восстановлен"
                print(f"➡️ {year} завершён, автоматически переходим к следующему году")
            else:
                statuses[year] = "нужен повторный запуск"
                print(
                    "⚠️ Год отмечен неполным; продолжаем следующий год, "
                    "а проблемные даты останутся в repair-отчёте"
                )
        summary_path, dates_path = self.write_repair_summary(statuses)
        print(f"\nОбщий repair-отчёт: {summary_path}")
        print(f"Неполные даты: {dates_path}")
        return statuses


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Скачать PDF НРАТ за 1991–2025 или безопасно восстановить "
            "отсутствующие файлы в существующей коллекции."
        )
    )
    parser.add_argument("--start-year", type=int, default=1991)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument(
        "--oldest-first",
        action="store_true",
        help="Идти от старых годов к новым (по умолчанию: от 2025 к 1991).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Папка для ZIP; по умолчанию автоматически находится соседняя 'nrat pdfs'.",
    )
    parser.add_argument(
        "--keep-staging",
        action="store_true",
        help="Не удалять распакованные PDF после успешного создания ZIP.",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help=(
            "Сверить каждый doc_id с существующими ZIP и создать отдельные "
            "*-repair-*.zip только с отсутствующими PDF. Старые ZIP не изменяются."
        ),
    )
    parser.add_argument(
        "--force-reaudit",
        action="store_true",
        help="В режиме --repair повторно проверить годы с готовым repair-отчётом.",
    )
    return parser


def configure_console_encoding() -> None:
    """Разрешить русские сообщения в старой Windows-консоли."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (LookupError, OSError):
                pass


def main(argv: Iterable[str] | None = None) -> int:
    configure_console_encoding()
    args = build_argument_parser().parse_args(argv)
    settings = Settings(
        start_year=args.start_year,
        end_year=args.end_year,
        newest_first=not args.oldest_first,
        output_dir=args.output_dir or find_default_output_dir(),
        delete_staging_after_zip=not args.keep_staging,
    )
    if args.force_reaudit and not args.repair:
        raise SystemExit("--force-reaudit применяется только вместе с --repair")
    if args.repair:
        runner: NratDownloader = NratRepairer(settings)
        statuses = runner.run_repair_all(force_reaudit=args.force_reaudit)
    else:
        runner = NratDownloader(settings)
        statuses = runner.run_all()
    print("\nИтог по годам:")
    for year, status in statuses.items():
        print(f"  {year}: {status}")
    return 0 if all(status != "нужен повторный запуск" for status in statuses.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
