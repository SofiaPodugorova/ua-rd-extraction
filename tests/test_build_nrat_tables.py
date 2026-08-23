import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))

import build_nrat_tables as tables
import extract_rd_data as extractor


class NratTableBuilderTests(unittest.TestCase):
    def test_doc_id_is_read_from_canonical_and_duplicate_names(self):
        doc_id = "ae9d5595593d62eefa1687551497db39"
        self.assertEqual(
            tables.doc_id_from_name(f"0222U001232_{doc_id}.pdf"), doc_id
        )
        self.assertEqual(
            tables.doc_id_from_name(f"0222U001232_{doc_id}(1).pdf"), doc_id
        )
        self.assertEqual(
            tables.doc_id_from_name(f"2_0224U031550_{doc_id}.pdf"), doc_id
        )

    def test_registration_is_found_after_variant_prefix(self):
        self.assertEqual(
            tables.registration_from_name("2_0224U031550_deadbeef.pdf"),
            "0224U031550",
        )

    def test_archive_members_are_grouped_by_doc_id(self):
        first = "a" * 32
        second = "b" * 32
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "sample.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(f"2022/a/0222U000001_{first}.pdf", b"one")
                archive.writestr(f"2022/a/0222U000001_{first}(1).pdf", b"two")
                archive.writestr(f"2022/a/0222U000002_{second}.pdf", b"three")
                archive.writestr("2022/a/unparsed.pdf", b"four")
                archive.writestr("manifest.json", "{}")

            with zipfile.ZipFile(archive_path) as archive:
                grouped, unparsed = tables.collect_archive_members(archive)

        self.assertEqual(sorted(grouped), [first, second])
        self.assertEqual(len(grouped[first]), 2)
        self.assertEqual(unparsed, ["2022/a/unparsed.pdf"])

    def test_complete_output_requires_matching_row_and_column_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "2000.zip"
            csv_path = root / "output_2000.csv"
            report_path = root / "output_2000.json"
            archive.write_bytes(b"zip marker")
            csv_path.write_text("header\n", encoding="utf-8")
            report_path.write_text(
                json.dumps(
                    {
                        "sample_mode": False,
                        "rows_written": 10,
                        "archive_unique_doc_ids": 10,
                        "csv_columns": len(tables.CSV_COLUMNS),
                        "archive": str(archive.resolve()),
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(
                tables.existing_output_is_complete(
                    archive_path=archive,
                    csv_path=csv_path,
                    report_path=report_path,
                )
            )

    def test_current_organization_label_does_not_leak_into_name(self):
        section = """Повне найменування юридичної особи (або ПІБ фізичної особи): Організація
Код за ЄДРПОУ: 12345678
Місцезнаходження: Київ
Форма власності: державна
Сфера управління: Міністерство
Ідентифікатор ROR: https://ror.org/example
Розмір організації: велика
Телефон: 123
"""
        record = extractor.parse_section_iii(section)
        self.assertEqual(record["performer_name"], "Організація")
        self.assertEqual(record["performer_ownership"], "державна")
        self.assertEqual(record["performer_ror"], "https://ror.org/example")
        self.assertEqual(record["performer_size"], "велика")

    def test_blank_current_organization_name_stays_blank(self):
        section = """Повне найменування юридичної особи (або ПІБ фізичної особи):
Код за ЄДРПОУ: 12345678
"""
        record = extractor.parse_section_iii(section)
        self.assertEqual(record["performer_name"], "")

    def test_all_co_performers_are_preserved(self):
        section = """Повне найменування юридичної особи (або ПІБ фізичної особи): Перша
Код за ЄДРПОУ: 11111111
Внесок співвиконавця у звітний етап: аналіз
Повне найменування юридичної особи (або ПІБ фізичної особи): Друга
Код за ЄДРПОУ: 22222222
Внесок співвиконавця у звітний етап: перевірка
"""
        parsed = json.loads(extractor.parse_section_iv(section)["co_performers"])
        self.assertEqual([row["name"] for row in parsed], ["Перша", "Друга"])
        self.assertEqual(parsed[1]["contribution"], "перевірка")

    def test_orcid_and_all_research_leaders_are_preserved(self):
        section = """Керівники роботи
Власне Прізвище Ім'я По-батькові: Перша Особа
Науковий ступінь: к.н.
Наукове звання: доцент
Ідентифікатор ORCID ID: 0000-0001
Додаткова інформація:
Власне Прізвище Ім'я По-батькові: Друга Особа
Науковий ступінь: д.н.
Наукове звання: професор
Ідентифікатор ORCID ID: 0000-0002
Додаткова інформація:
"""
        record = extractor.parse_section_vii(section)
        leaders = json.loads(record["research_leaders"])
        self.assertEqual(record["pi_orcid"], "0000-0001")
        self.assertEqual(len(leaders), 2)
        self.assertEqual(leaders[1]["orcid"], "0000-0002")

    def test_section_x_keeps_head_credentials_and_preparer(self):
        section = """Керівник юридичної особи
Керівник Особа
д. н., 01.01.01
Перелік осіб-виконавців
Виконавець Особа
Відповідальний за підготовку
облікових документів
Укладач Особа
Телефон
+380111111
Реєстратор
Посада реєстратора
"""
        record = extractor.parse_section_x(section)
        self.assertEqual(record["org_head"], "Керівник Особа\nд. н., 01.01.01")
        self.assertEqual(record["document_preparer"], "Укладач Особа")
        self.assertEqual(record["document_preparer_phone"], "+380111111")


if __name__ == "__main__":
    unittest.main()
