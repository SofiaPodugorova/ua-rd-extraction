import unittest

import audit_duplicate_publications as audit


def variant(
    *,
    readable=True,
    text="text",
    render="render",
    registration="0225U000001",
    filename_registration="0225U000001",
    state="0124U000001",
    title="Назва",
):
    return {
        "readable": readable,
        "text_sha256": text,
        "render_sha256": render,
        "registration_internal": registration,
        "registration_filename": filename_registration,
        "state_registration": state,
        "title_uk": title,
        "title_en": "",
    }


class DuplicatePublicationAuditTests(unittest.TestCase):
    def test_identical_text_and_render_is_one_publication(self):
        classification, same, _ = audit.classify([variant(), variant()])
        self.assertEqual(
            classification, "same_publication_identical_text_and_render"
        )
        self.assertIs(same, True)

    def test_one_bad_copy_with_same_registration_is_one_publication(self):
        good = variant()
        bad = variant(readable=False, text="", render="", registration="")
        classification, same, _ = audit.classify([good, bad])
        self.assertEqual(
            classification, "same_publication_one_variant_unreadable"
        )
        self.assertIs(same, True)

    def test_different_internal_records_are_not_collapsed(self):
        first = variant(text="one", render="one", registration="0225U1", title="A")
        second = variant(text="two", render="two", registration="0225U2", title="B")
        classification, same, _ = audit.classify([first, second])
        self.assertEqual(classification, "different_publications_same_doc_id")
        self.assertIs(same, False)

    def test_uncertain_bad_copy_is_not_collapsed(self):
        good = variant(filename_registration="0225U000001")
        bad = variant(
            readable=False,
            text="",
            render="",
            registration="",
            filename_registration="0225U999999",
        )
        classification, same, _ = audit.classify([good, bad])
        self.assertEqual(classification, "ambiguous_unreadable_variant")
        self.assertIsNone(same)


if __name__ == "__main__":
    unittest.main()
