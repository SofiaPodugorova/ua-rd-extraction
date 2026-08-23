import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from download_nrat_pdfs import NratDownloader, NratRepairer, Settings


DOC_A = "a" * 32
DOC_B = "b" * 32
DOC_C = "c" * 32


def result(doc_id: str) -> dict[str, str]:
    return {
        "registration": "0123U000001",
        "detail_url": f"https://example.test/{doc_id}",
        "pdf_url": f"https://example.test/pdf/{doc_id}",
        "title": "test",
        "doc_id": doc_id,
    }


def result_page(date: str, total: int) -> BeautifulSoup:
    return BeautifulSoup(
        f"""
        <html><body>
          <input name="dateFromSearch" value="{date}">
          <input name="dateToSearch" value="{date}">
          <p>Знайдено документів: {total}</p>
        </body></html>
        """,
        "html.parser",
    )


class RepairTests(unittest.TestCase):
    def settings(self, root: Path) -> Settings:
        return Settings(
            start_year=2025,
            end_year=2025,
            output_dir=root,
            delete_staging_after_zip=False,
        )

    def test_archive_index_deduplicates_doc_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with zipfile.ZipFile(root / "2025-old.zip", "w") as archive:
                archive.writestr(f"2025/x_1_{DOC_A}.pdf", b"%PDF-a")
                archive.writestr(f"2025/x_2_{DOC_A}.pdf", b"%PDF-a-copy")
                archive.writestr(f"2025/x_{DOC_B}.pdf", b"%PDF-b")

            repairer = NratRepairer(self.settings(root))
            doc_ids, archives = repairer.archive_pdf_ids(2025)

            self.assertEqual(doc_ids, {DOC_A, DOC_B})
            self.assertEqual(archives, ["2025-old.zip"])

    def test_google_drive_style_year_folder_is_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            day = root / "2025" / "2025-01" / "2025-01-01"
            day.mkdir(parents=True)
            (day / f"x_{DOC_A}.pdf").write_bytes(b"%PDF-existing")

            repairer = NratRepairer(self.settings(root))

            self.assertEqual(set(repairer.loose_existing_pdf_map(2025)), {DOC_A})

    def test_normal_mode_never_treats_patch_zip_as_full_year(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with zipfile.ZipFile(root / "2025-repair-test.zip", "w") as archive:
                archive.writestr(f"2025/x_{DOC_A}.pdf", b"%PDF-a")

            downloader = NratDownloader(self.settings(root))
            repairer = NratRepairer(self.settings(root))

            self.assertEqual(downloader.archives_for_year(2025), [])
            self.assertEqual(
                [path.name for path in repairer.archives_for_year(2025)],
                ["2025-repair-test.zip"],
            )

    def test_zero_publication_year_skips_all_365_daily_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(start_year=1991, end_year=1991, output_dir=root)
            repairer = NratRepairer(settings)
            repairer.refresh_token = lambda: None  # type: ignore[method-assign]
            repairer.annual_total = lambda year: 0  # type: ignore[method-assign]

            def daily_request_must_not_run(*args, **kwargs):
                raise AssertionError("пустой год не должен проверяться по дням")

            repairer.repair_day = daily_request_must_not_run  # type: ignore[method-assign]

            self.assertTrue(repairer.run_repair_year(1991))
            report = repairer.report_path(1991).read_text(encoding="utf-8")
            self.assertIn('"zero_year_fast_path": true', report)
            self.assertIn('"annual_site_total": 0', report)

    def test_full_repair_continues_after_incomplete_year_and_writes_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(start_year=1999, end_year=2000, output_dir=root)
            repairer = NratRepairer(settings)
            repairer._atomic_write_json(
                repairer.report_path(1999),
                {
                    "year": 1999,
                    "complete": False,
                    "unfinished_dates": ["1999-12-19"],
                    "unresolved_doc_ids": [],
                },
            )
            calls: list[int] = []

            def fake_year(year: int, force_reaudit: bool = False) -> bool:
                calls.append(year)
                return year == 2000

            repairer.run_repair_year = fake_year  # type: ignore[method-assign]
            statuses = repairer.run_repair_all()

            self.assertEqual(calls, [2000, 1999])
            self.assertEqual(statuses[1999], "нужен повторный запуск")
            summary = root / "_repair_summary_1999_2000.json"
            incomplete = root / "_repair_incomplete_dates_1999_2000.txt"
            self.assertTrue(summary.is_file())
            self.assertIn("1999-12-19", incomplete.read_text(encoding="utf-8"))

    def test_repair_day_uses_all_pa_pages_and_skips_existing_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repairer = NratRepairer(self.settings(root))
            pages: list[int] = []

            def fake_page(date_from: str, date_to: str, page: int):
                pages.append(page)
                soup = result_page(date_from, 3)
                if page == 1:
                    return [result(DOC_A), result(DOC_B)], soup
                return [result(DOC_C)], soup

            def fake_download(url: str, registration: str, doc_id: str, folder: Path):
                if doc_id == DOC_B:
                    return "ok", folder / f"x_{doc_id}.pdf", None
                return "notpdf", None, "404"

            repairer.get_search_results_page = fake_page  # type: ignore[method-assign]
            repairer.download_pdf = fake_download  # type: ignore[method-assign]
            available = {DOC_A}

            with patch("download_nrat_pdfs.time.sleep"):
                record, stats, complete = repairer.repair_day(
                    2025, "2025-01-01", "2025-01-01", available, set()
                )

            self.assertTrue(complete)
            self.assertEqual(pages, [1, 2])
            self.assertEqual(record["doc_ids"], [DOC_A, DOC_B, DOC_C])
            self.assertEqual(record["no_pdf_doc_ids"], [DOC_C])
            self.assertEqual(stats["already"], 1)
            self.assertEqual(stats["ok"], 1)
            self.assertIn(DOC_B, available)

    def test_repeated_page_never_completes_day(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repairer = NratRepairer(self.settings(root))

            def fake_page(date_from: str, date_to: str, page: int):
                return [result(DOC_A)], result_page(date_from, 2)

            repairer.get_search_results_page = fake_page  # type: ignore[method-assign]
            with patch("download_nrat_pdfs.time.sleep"):
                _, _, complete = repairer.repair_day(
                    2025, "2025-01-01", "2025-01-01", {DOC_A}, set()
                )

            self.assertFalse(complete)
            error_text = repairer.error_log_path(2025).read_text(encoding="utf-8")
            self.assertIn("page_repeat", error_text)
            self.assertIn("count_mismatch", error_text)

    def test_html_with_http_200_is_retried_and_never_confirmed_as_notpdf(self) -> None:
        class HtmlResponse:
            status_code = 200
            headers: dict[str, str] = {}

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def iter_content(chunk_size: int):
                return [b"<html>temporary error</html>"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repairer = NratRepairer(self.settings(root))
            calls = 0

            def fake_get(*args, **kwargs):
                nonlocal calls
                calls += 1
                return HtmlResponse()

            repairer.session.get = fake_get  # type: ignore[method-assign]
            with patch("download_nrat_pdfs.time.sleep"):
                status, _, _ = repairer.download_pdf(
                    "https://example.test/pdf", "0123U000001", DOC_A, root
                )

            self.assertEqual(calls, repairer.settings.retry_count)
            self.assertEqual(status, "fail")

    def test_http_500_is_terminal_and_not_retried(self) -> None:
        class ServerErrorResponse:
            status_code = 500

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repairer = NratRepairer(self.settings(root))
            calls = 0

            def fake_get(*args, **kwargs):
                nonlocal calls
                calls += 1
                return ServerErrorResponse()

            repairer.session.get = fake_get  # type: ignore[method-assign]
            with patch("download_nrat_pdfs.time.sleep"):
                status, _, reason = repairer.download_pdf(
                    "https://example.test/pdf", "0123U000001", DOC_A, root
                )

            self.assertEqual(calls, 1)
            self.assertEqual(status, "server500")
            self.assertIn("500 Server Error", reason or "")

    def test_known_http_500_is_not_downloaded_and_day_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repairer = NratRepairer(self.settings(root))
            repairer.get_search_results_page = (  # type: ignore[method-assign]
                lambda date_from, date_to, page: (
                    [result(DOC_A), result(DOC_B)],
                    result_page(date_from, 2),
                )
            )

            def download_must_not_run(*args, **kwargs):
                raise AssertionError("известный HTTP 500 нельзя запрашивать повторно")

            repairer.download_pdf = download_must_not_run  # type: ignore[method-assign]
            with patch("download_nrat_pdfs.time.sleep"):
                record, stats, complete = repairer.repair_day(
                    2025,
                    "2025-01-01",
                    "2025-01-01",
                    {DOC_A},
                    {DOC_B},
                )

            self.assertTrue(complete)
            self.assertEqual(record["server500_doc_ids"], [DOC_B])
            self.assertEqual(stats["server500"], 1)

    def test_legacy_pure_http_500_day_is_migrated_without_site_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repairer = NratRepairer(self.settings(root))
            repairer.log_error(
                2025,
                "2025-01-02",
                "0123U000001",
                DOC_B,
                "fail",
                "500 Server Error: Internal Server Error",
                "https://example.test/pdf",
            )
            repairer.existing_pdf_ids_by_date = (  # type: ignore[method-assign]
                lambda year: {"2025-01-02": {DOC_A}}
            )
            state = {"version": 1, "year": 2025, "days": {}}

            migrated = repairer.migrate_legacy_server500_days(2025, state)

            self.assertEqual(migrated, ["2025-01-02"])
            self.assertEqual(
                state["days"]["2025-01-02"]["doc_ids"], [DOC_A, DOC_B]
            )
            self.assertEqual(
                state["days"]["2025-01-02"]["server500_doc_ids"], [DOC_B]
            )

    def test_legacy_http_500_day_with_page_error_is_migrated_and_marked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repairer = NratRepairer(self.settings(root))
            repairer.log_error(
                2025,
                "2025-01-02",
                "0123U000001",
                DOC_B,
                "fail",
                "500 Server Error: Internal Server Error",
                "https://example.test/pdf",
            )
            repairer.log_error(
                2025,
                "2025-01-02",
                "-",
                "-",
                "page_fail",
                "страница выдачи не загрузилась",
                "https://example.test/search",
            )
            state = {"version": 1, "year": 2025, "days": {}}

            migrated = repairer.migrate_legacy_server500_days(2025, state)

            self.assertEqual(migrated, ["2025-01-02"])
            self.assertEqual(
                state["days"]["2025-01-02"]["additional_logged_statuses"],
                ["page_fail"],
            )

    def test_patch_zip_contains_only_ids_missing_from_old_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with zipfile.ZipFile(root / "2025-old.zip", "w") as archive:
                archive.writestr(f"2025/2025-01/2025-01-01/x_{DOC_A}.pdf", b"%PDF-a")

            repairer = NratRepairer(self.settings(root))
            day = repairer.year_dir(2025) / "2025-01" / "2025-01-01"
            day.mkdir(parents=True)
            (day / f"x_{DOC_B}.pdf").write_bytes(b"%PDF-b")
            archived, _ = repairer.archive_pdf_ids(2025)

            patch_path = repairer.finalize_patch_archive(
                2025,
                {DOC_A, DOC_B},
                archived,
                {"year": 2025, "complete": True},
            )

            self.assertIsNotNone(patch_path)
            with zipfile.ZipFile(patch_path) as archive:
                pdf_names = [
                    name for name in archive.namelist() if name.lower().endswith(".pdf")
                ]
                self.assertEqual(len(pdf_names), 1)
                self.assertIn(DOC_B, pdf_names[0])
                self.assertTrue(any(name.endswith("_repair_manifest.json") for name in archive.namelist()))


if __name__ == "__main__":
    unittest.main()
