"""Naming from the paper's own metadata, and the hash index that finds the md again. No network."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pdf2md_local as pdf2md  # noqa: E402

SAMPLE_MD = """---
source_pdf: "hollis.pdf"
tool: mineru
---
# The value of feedback in project-based learning settings: The case of science classrooms

J. Hollis $^{a,b,*}$ , K. Park $^{a}$ , N.R. Diaz $^{c}$

## ABSTRACT

Received 5 April 2012. See doi.org/10.5555/example.2012.026 for details.

# References

Other, A. (2001). doi:10.9999/not-this-one
"""

RIVERA_MD = """# A Gentle Introduction to Bayesian Networks For Teachers and Researchers

Maria Rivera University of Washington

Tom Lee University of Macau
"""


class MdParsingTest(unittest.TestCase):
    def test_title_author_year_offline(self):
        meta = pdf2md.detect_metadata(Path("hollis.pdf"), SAMPLE_MD, online=False)
        self.assertEqual(meta["surname"], "Hollis")
        self.assertEqual(meta["year"], "2012")
        self.assertEqual(pdf2md.name_from_metadata(meta), "hollis_2012_value-feedback-project-based")

    def test_affiliation_is_not_a_surname(self):
        head = pdf2md.md_head(RIVERA_MD)
        self.assertEqual(pdf2md.md_first_author(head, pdf2md.md_title(head)), "Rivera")

    def test_reference_list_is_cut_off(self):
        head = pdf2md.md_head(SAMPLE_MD)
        self.assertIn("10.5555/example.2012.026", head)
        self.assertNotIn("10.9999", head)

    def test_year_from_file_name_or_arxiv_id(self):
        md = "# A generalized rating tree model\n\nAlex Kim\n"
        self.assertEqual(pdf2md.detect_metadata(Path("kim2015.pdf"), md, online=False)["year"], "2015")
        self.assertEqual(pdf2md.arxiv_id_for(Path("2101.01234v1.pdf"), ""), "2101.01234")
        self.assertEqual(pdf2md.offline_year(Path("2101.01234v1.pdf"), "", "2101.01234"), "2021")

    def test_incomplete_metadata_gives_no_name(self):
        self.assertIsNone(pdf2md.name_from_metadata({"title": "Some long title here", "surname": None, "year": "2020"}))
        self.assertIsNone(pdf2md.name_from_metadata({"title": "短標題", "surname": "Kim", "year": "2020"}))

    def test_titles_match(self):
        self.assertTrue(pdf2md.titles_match("The value of feedback in project-based learning settings",
                                            "The value of feedback in project-based learning settings: The case"))
        self.assertFalse(pdf2md.titles_match("Variational inference for rating scales", "Score-based tests of item fit"))

    def test_crossref_prefers_print_year(self):
        record = pdf2md.crossref_record({"title": ["T"], "author": [{"family": "Okafor"}], "DOI": "10.1/ABC",
                                         "issued": {"date-parts": [[2020, 5]]},
                                         "published-print": {"date-parts": [[2021, 3]]}})
        self.assertEqual((record["surname"], record["year"], record["doi"]), ("Okafor", "2021", "10.1/abc"))


class OnlineChoiceTest(unittest.TestCase):
    def test_doi_from_reference_list_never_renames(self):
        wrong = {"title": "Completely different paper about trees", "surname": "Other", "year": "2001", "doi": "x"}
        with mock.patch.object(pdf2md, "crossref_by_doi", return_value=wrong), \
                mock.patch.object(pdf2md, "crossref_by_title", return_value=[]), \
                mock.patch.object(pdf2md, "pdf_embedded_dois", return_value=[]):
            meta = pdf2md.detect_metadata(Path("hollis.pdf"), SAMPLE_MD, online=True)
        self.assertEqual(meta["source"], "md")

    def test_matching_doi_wins(self):
        right = {"title": "The value of feedback in project-based learning settings: The case of science classrooms",
                 "surname": "Hollis", "year": "2013", "doi": "10.5555/example.2012.026"}
        with mock.patch.object(pdf2md, "crossref_by_doi", return_value=right), \
                mock.patch.object(pdf2md, "pdf_embedded_dois", return_value=[]):
            meta = pdf2md.detect_metadata(Path("hollis.pdf"), SAMPLE_MD, online=True)
        self.assertEqual(pdf2md.name_from_metadata(meta), "hollis_2013_value-feedback-project-based")

    def test_zotero_names_keep_the_frozen_rule(self):
        pdf = Path("Okafor et al. - 2021 - Variational inference for multidimensional rating scal.pdf")
        name, _, how = pdf2md.choose_name(pdf, "# Variational inference for multidimensional rating scales\n",
                                          {"metadata_lookup": False})
        self.assertEqual(name, "okafor_2021_variational-inference-multidimensional-rating")
        self.assertIn("Zotero", how)


class IndexTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        self.inbox, self.output = root / "inbox", root / "output"
        self.inbox.mkdir()
        self.output.mkdir()
        self.log_file = self.inbox / pdf2md.LOG_NAME
        self.config = {"metadata_lookup": False}

    def tearDown(self):
        self._temp.cleanup()

    def convert(self, pdf, md_text):
        def fake_mineru(config, pdf_path, work_dir):
            md = work_dir / "markdown.md"
            md.write_text(md_text, encoding="utf-8")
            return md
        with mock.patch.object(pdf2md, "convert_with_mineru", side_effect=fake_mineru):
            return pdf2md.process_one(self.config, pdf, "mineru", "0", self.output, self.inbox / "done", self.log_file)

    def test_named_from_metadata_and_found_again_by_hash(self):
        pdf = self.inbox / "hollis.pdf"
        pdf.write_bytes(b"%PDF hollis")
        self.assertEqual(self.convert(pdf, pdf2md.md_body(SAMPLE_MD)), "done")
        name = "hollis_2012_value-feedback-project-based"
        md = self.output / name / f"{name}.md"
        self.assertTrue(md.exists())
        self.assertIn('named_from: "md"', md.read_text(encoding="utf-8"))
        moved = self.inbox / "done" / "hollis.pdf"
        self.assertTrue(moved.exists())
        # The same bytes under any other name find the same md
        renamed = self.inbox / "whatever.pdf"
        renamed.write_bytes(moved.read_bytes())
        self.assertEqual(pdf2md.find_md(renamed, self.output), md)
        self.assertEqual(self.convert(renamed, "# x\n"), "skipped")

    def test_same_paper_other_file_is_not_converted_twice(self):
        first = self.inbox / "hollis.pdf"
        first.write_bytes(b"%PDF one")
        self.convert(first, pdf2md.md_body(SAMPLE_MD))
        second = self.inbox / "hollis_copy.pdf"
        second.write_bytes(b"%PDF two, different bytes")
        self.assertEqual(self.convert(second, pdf2md.md_body(SAMPLE_MD)), "skipped")
        self.assertEqual(len([p for p in self.output.iterdir() if p.is_dir()]), 1)
        self.assertIsNotNone(pdf2md.find_md(second, self.output))

    def test_name_clash_with_different_paper_gets_suffix(self):
        taken = self.output / "hollis_2012_value-feedback-project-based"
        taken.mkdir()
        (taken / f"{taken.name}.md").write_text("# Something else entirely, not the same\n", encoding="utf-8")
        pdf = self.inbox / "hollis.pdf"
        pdf.write_bytes(b"%PDF")
        self.assertEqual(self.convert(pdf, pdf2md.md_body(SAMPLE_MD)), "done")
        self.assertTrue((self.output / f"{taken.name}_2" / f"{taken.name}_2.md").exists())
        self.assertEqual((taken / f"{taken.name}.md").read_text(encoding="utf-8"), "# Something else entirely, not the same\n")

    def test_unreadable_metadata_falls_back_to_file_name(self):
        pdf = self.inbox / "notes 2.pdf"
        pdf.write_bytes(b"%PDF")
        self.assertEqual(self.convert(pdf, "no heading here\n"), "done")
        self.assertTrue((self.output / "notes_2" / "notes_2.md").exists())

    def test_old_capitalized_folder_is_still_found(self):
        old = self.output / "Hollis"
        old.mkdir()
        (old / "Hollis.md").write_text("# x\n", encoding="utf-8")
        self.assertEqual(pdf2md.existing_folder_md(self.output, "hollis"), old / "Hollis.md")
        self.assertIsNotNone(pdf2md.find_md(self.inbox / "hollis.pdf", self.output))

    def test_volume_issue_year_wins(self):
        head = "# 某篇論文的標題\n\nReceived 2023\n\n測驗學刊, 71(1), 2024\n"
        self.assertEqual(pdf2md.offline_year(Path("x.pdf"), head, None), "2024")

    def test_renamed_copy_is_found_by_hash(self):
        pdf = self.inbox / "hollis.pdf"
        pdf.write_bytes(b"%PDF hollis")
        sha = pdf2md.file_sha256(pdf)
        self.convert(pdf, pdf2md.md_body(SAMPLE_MD))
        found = pdf2md.find_md(Path("elsewhere/some other name.pdf"), self.output, sha)
        self.assertEqual(found.parent.name, "hollis_2012_value-feedback-project-based")


if __name__ == "__main__":
    unittest.main()
