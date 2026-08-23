import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from consolidate_nrat_archives import Consolidator, doc_id_from_name


DOC_A = "a" * 32
DOC_B = "b" * 32
DOC_C = "c" * 32


class ConsolidationTests(unittest.TestCase):
    def test_multiple_archives_and_staging_become_one_verified_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with zipfile.ZipFile(root / "2025-old.zip", "w") as archive:
                archive.writestr(
                    f"2025/2025-01/2025-01-01/x_{DOC_A}.pdf", b"%PDF-a"
                )
                archive.writestr(
                    f"2025/2025-01/2025-01-02/x_{DOC_B}.pdf", b"%PDF-b"
                )
            with zipfile.ZipFile(root / "2025-repair.zip", "w") as archive:
                archive.writestr(
                    f"2025/2025-01/2025-01-01/copy_{DOC_A}.pdf", b"%PDF-a"
                )
            staging = root / ".nrat_repair_work" / "2025" / "2025-01" / "2025-01-03"
            staging.mkdir(parents=True)
            (staging / f"x_{DOC_C}.pdf").write_bytes(b"%PDF-c")

            consolidator = Consolidator(root, 2025, 2025)
            results = consolidator.run()

            canonical = root / "2025.zip"
            self.assertTrue(canonical.is_file())
            self.assertFalse((root / "2025-old.zip").exists())
            self.assertFalse((root / "2025-repair.zip").exists())
            self.assertFalse((root / ".nrat_repair_work" / "2025").exists())
            with zipfile.ZipFile(canonical) as archive:
                self.assertIsNone(archive.testzip())
                ids = {
                    doc_id
                    for name in archive.namelist()
                    if name.lower().endswith(".pdf")
                    for doc_id in [doc_id_from_name(name)]
                    if doc_id
                }
            self.assertEqual(ids, {DOC_A, DOC_B, DOC_C})
            self.assertEqual(results["2025"]["source_pdf_entries"], 4)
            self.assertEqual(results["2025"]["output_pdf_entries"], 3)
            self.assertEqual(
                results["2025"]["identical_duplicate_entries_removed"], 1
            )
            report = json.loads(consolidator.report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["years"]["2025"]["output_unique_doc_ids"], 3)

    def test_empty_year_gets_manifest_only_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = Consolidator(root, 1991, 1991).run()

            with zipfile.ZipFile(root / "1991.zip") as archive:
                self.assertEqual(
                    archive.namelist(), ["1991/_consolidation_manifest.json"]
                )
            self.assertEqual(results["1991"]["output_unique_doc_ids"], 0)


if __name__ == "__main__":
    unittest.main()
