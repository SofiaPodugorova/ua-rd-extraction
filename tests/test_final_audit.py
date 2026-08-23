import json
import tempfile
import unittest
from pathlib import Path

import final_audit_1991_2025 as audit


class FinalAuditTests(unittest.TestCase):
    def test_persistent_count_mismatches_include_server500_dates(self):
        report = {
            "unfinished_dates": ["2025-07-24"],
            "accepted_server500_dates_with_additional_errors": {
                "2025-01-30": ["count_mismatch"]
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "errors.txt"
            log.write_text(
                "date | day | status | reg | id | reason | url\n"
                "x | 2025-01-30 | count_mismatch | - | - | "
                "сайт объявил 71, собрано уникальных doc_id 70 | url\n"
                "x | 2025-07-24 | count_mismatch | - | - | "
                "сайт объявил 13, собрано уникальных doc_id 12 | url\n",
                encoding="utf-8",
            )
            self.assertEqual(
                audit.persistent_count_mismatches(report, log),
                {"2025-01-30": 1, "2025-07-24": 1},
            )

    def test_repair_sets_keep_error_categories_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = Path(tmp) / "progress.json"
            progress.write_text(
                json.dumps(
                    {
                        "days": {
                            "2025-01-01": {
                                "doc_ids": ["a", "b", "c"],
                                "no_pdf_doc_ids": ["b"],
                                "server500_doc_ids": ["c"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            site, no_pdf, server500 = audit.repair_sets(progress)
            self.assertEqual(site, {"a", "b", "c"})
            self.assertEqual(no_pdf, {"b"})
            self.assertEqual(server500, {"c"})


if __name__ == "__main__":
    unittest.main()
