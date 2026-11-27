"""Naming from the paper's own metadata, and the hash index that finds the md again. No network."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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


def xmp(title=None, doi=None):
    parts = [b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description>']
    if title:
        parts.append(b"<dc:title><rdf:Alt><rdf:li xml:lang=\"x-default\">" + title + b"</rdf:li></rdf:Alt></dc:title>")
    if doi:
        parts.append(b"<prism:doi>" + doi + b"</prism:doi>")
    parts.append(b"</rdf:Description></rdf:RDF>")
    return b"".join(parts)


class DoiMatchTest(unittest.TestCase):
    """Finding an already converted paper by DOI before conversion."""

    TITLE = "Gibbs sampling for rating scale models with many dimensions"

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        self.output = root / "output"
        folder = self.output / "okafor_2024_gibbs-sampling-rating"
        folder.mkdir(parents=True)
        self.md = folder / "okafor_2024_gibbs-sampling-rating.md"
        self.md.write_text(f"# {self.TITLE}\n", encoding="utf-8")
        pdf2md.add_index_entry(self.output, {"sha256": "0" * 64, "folder": folder.name, "doi": "10.5555/example.2024.321",
                                             "title": self.TITLE, "source_pdf": "okafor.pdf"})
        self.pdf = root / "copy.pdf"

    def tearDown(self):
        self._temp.cleanup()

    def found(self, data):
        self.pdf.write_bytes(b"%PDF-1.7\n" + data)
        return pdf2md.find_existing_by_doi(self.output, self.pdf)

    def test_figure_xmp_title_does_not_veto_the_papers_own_doi(self):
        figure = xmp(b"Adobe Illustrator Artwork")
        paper = xmp(self.TITLE.encode(), b"10.5555/example.2024.321")
        self.assertEqual(self.found(figure + b"\n" + paper), self.md)
        self.assertEqual(self.found(xmp(b"R Graphics Output", b"10.5555/example.2024.321")), self.md)

    def test_doi_with_another_title_in_the_same_packet_is_not_a_duplicate(self):
        self.assertIsNone(self.found(xmp(b"A completely different paper about rating scales", b"10.5555/example.2024.321")))

    def test_reference_link_alone_is_not_a_duplicate(self):
        link = b"<< /S /URI /URI (https://doi.org/10.5555/example.2024.321) >>"
        self.assertIsNone(self.found(link))
        self.assertIsNone(self.found(link + b"<< /Title (Some other paper on Bayesian methods) >>"))

    def test_link_with_matching_info_title_is_a_duplicate(self):
        link = b"<< /S /URI /URI (https://doi.org/10.5555/example.2024.321) >>"
        info = b"<< /Title (" + self.TITLE.encode() + b") /Producer (pdfTeX) >>"
        self.assertEqual(self.found(link + info), self.md)

    def test_info_title_strings_are_decoded(self):
        self.assertEqual(pdf2md.info_title(rb"<< /Title (Volume 29 \(2\): a (nested) title) >>"),
                         "Volume 29 (2): a (nested) title")
        self.assertEqual(pdf2md.info_title(rb"<< /Title (Children\222s reading \
in three schools) >>"), "Children\x92s reading in three schools")
        utf16 = ("\ufeff" + "Ålund et al. on ordinal data").encode("utf-16-be")
        self.assertEqual(pdf2md.info_title(b"<< /Title <" + utf16.hex().encode() + b"> >>"), "Ålund et al. on ordinal data")
        self.assertIsNone(pdf2md.info_title(b"<< /Title (Microsoft Word - final_v2.docx) >>"))
        self.assertIsNone(pdf2md.info_title(b"<< /Title (Introduction) >>"))

    def test_info_dictionary_doi_is_paired_with_its_title(self):
        info = b"<< /Title (" + self.TITLE.encode() + b") /doi (10.5555/example.2024.321) >>"
        self.assertEqual(pdf2md.pdf_metadata_records(self.write(info)), [("10.5555/example.2024.321", self.TITLE)])
        self.assertEqual(self.found(info), self.md)

    def write(self, data):
        self.pdf.write_bytes(b"%PDF-1.7\n" + data)
        return self.pdf



class LengthRuleTest(unittest.TestCase):
    """Same DOI and title but very different length: a summary and the paper are kept apart."""

    setUp, tearDown, convert = IndexTest.setUp, IndexTest.tearDown, IndexTest.convert

    TITLE = "Approximate inference for latent rating models using nested approximations"
    DOI = "10.5555/example.2009.700"

    def convert_with_meta(self, pdf, md_text):
        meta = {"title": self.TITLE, "doi": self.DOI, "surname": "Mendes", "year": "2009"}
        with mock.patch.object(pdf2md, "choose_name",
                               return_value=("mendes_2009_approximate-inference-latent", meta, "crossref-title")):
            return self.convert(pdf, md_text)

    def test_summary_and_paper_are_both_kept_and_doi_finds_the_paper(self):
        summary = self.inbox / "summary.pdf"
        summary.write_bytes(b"%PDF summary")
        self.assertEqual(self.convert_with_meta(summary, f"# {self.TITLE}\n\nA short review.\n" + "x" * 2000), "done")
        paper = self.inbox / "paper.pdf"
        paper.write_bytes(b"%PDF paper")
        self.assertEqual(self.convert_with_meta(paper, f"# {self.TITLE}\n\n" + "y" * 50000), "done")
        first = self.output / "mendes_2009_approximate-inference-latent"
        second = self.output / "mendes_2009_approximate-inference-latent_2"
        self.assertTrue((first / f"{first.name}.md").exists())
        self.assertTrue((second / f"{second.name}.md").exists())
        self.assertIn("Kept separately", self.log_file.read_text(encoding="utf-8"))
        # A DOI lookup now finds the longer md (the paper), not the summary
        self.assertEqual(pdf2md.find_md_by_doi(self.output, [self.DOI], self.TITLE).parent, second)

    def test_similar_length_is_still_the_same_paper(self):
        first = self.inbox / "preprint.pdf"
        first.write_bytes(b"%PDF preprint")
        self.assertEqual(self.convert_with_meta(first, f"# {self.TITLE}\n\n" + "y" * 40000), "done")
        second = self.inbox / "published.pdf"
        second.write_bytes(b"%PDF published")
        self.assertEqual(self.convert_with_meta(second, f"# {self.TITLE}\n\n" + "z" * 50000), "skipped")
        self.assertFalse((self.output / "mendes_2009_approximate-inference-latent_2").exists())

    def test_lengths_differ(self):
        self.assertTrue(pdf2md.lengths_differ(13000, 150000))
        self.assertFalse(pdf2md.lengths_differ(40000, 50000))
        self.assertFalse(pdf2md.lengths_differ(None, 50000))



class SupplementTest(unittest.TestCase):
    """An appendix, supplement or erratum is a document of its own, not a duplicate of its paper."""

    setUp, tearDown, convert = IndexTest.setUp, IndexTest.tearDown, IndexTest.convert
    TITLE = "Fast approximate inference for rating scale models with many dimensions"
    DOI = "10.5555/example.2026.290"
    NAME = "okafor_2026_fast-approximate-inference-rating"

    def convert_with_meta(self, pdf, md_text):
        meta = {"title": self.TITLE, "doi": self.DOI, "surname": "Okafor", "year": "2026"}
        with mock.patch.object(pdf2md, "choose_name", return_value=(self.NAME, meta, "crossref-title")):
            return self.convert(pdf, md_text)

    def test_appendix_gets_its_own_folder_and_doi_still_finds_the_paper(self):
        paper = self.inbox / "paper.pdf"
        paper.write_bytes(b"%PDF paper")
        self.assertEqual(self.convert_with_meta(paper, f"# {self.TITLE}\n\n" + "y" * 40000), "done")
        appendix = self.inbox / "appendix.pdf"
        appendix.write_bytes(b"%PDF appendix")
        # Long enough that the length rule alone would call it the same paper
        md = f"# Appendix to “{self.TITLE}”\n\n" + "z" * 30000
        self.assertEqual(self.convert_with_meta(appendix, md), "done")
        folder = self.output / f"{self.NAME}_appendix"
        self.assertTrue((folder / f"{folder.name}.md").exists())
        self.assertEqual(pdf2md.find_md_by_doi(self.output, [self.DOI], self.TITLE).parent.name, self.NAME)
        entry = next(e for e in pdf2md.load_index(self.output)["entries"] if e["folder"] == folder.name)
        self.assertIsNone(entry.get("doi"))
        self.assertTrue(entry["title"].startswith("Appendix to"))

    def test_appendix_converted_before_its_paper_leaves_the_name_to_the_paper(self):
        appendix = self.inbox / "appendix.pdf"
        appendix.write_bytes(b"%PDF appendix")
        self.assertEqual(self.convert_with_meta(appendix, f"# Supplementary material for {self.TITLE}\n\n" + "z" * 9000), "done")
        paper = self.inbox / "paper.pdf"
        paper.write_bytes(b"%PDF paper")
        self.assertEqual(self.convert_with_meta(paper, f"# {self.TITLE}\n\n" + "y" * 40000), "done")
        self.assertTrue((self.output / f"{self.NAME}_supplement").is_dir())
        self.assertTrue((self.output / self.NAME / f"{self.NAME}.md").exists())

    def test_kinds(self):
        self.assertEqual(pdf2md.supplement_kind("Online Appendix"), "appendix")
        self.assertEqual(pdf2md.supplement_kind("Supporting Information"), "supplement")
        self.assertEqual(pdf2md.supplement_kind("Erratum to: A paper"), "erratum")
        self.assertIsNone(pdf2md.supplement_kind("A supplement-free paper on rating scales"))



class RecheckTest(unittest.TestCase):
    """`recheck` finds PDFs that older rules skipped by mistake, and --apply sends them back."""

    setUp, tearDown, convert = IndexTest.setUp, IndexTest.tearDown, IndexTest.convert
    TITLE = "Variational inference for rating scale models with many dimensions"
    NAME = "okafor_2024_variational-inference-rating"

    def prepare(self):
        meta = {"title": self.TITLE, "doi": "10.5555/example.2024.11", "surname": "Okafor", "year": "2024"}
        paper = self.inbox / "paper.pdf"
        paper.write_bytes(b"%PDF the paper")
        with mock.patch.object(pdf2md, "choose_name", return_value=(self.NAME, meta, "crossref-doi")):
            self.assertEqual(self.convert(paper, f"# {self.TITLE}\n\n" + "y" * 20000), "done")
        skipped = self.inbox / "skipped"
        skipped.mkdir()
        files = {
            "same.pdf": b"%PDF the paper",
            "Other - 2020 - Something else entirely about item position effects.pdf": b"%PDF other",
            f"Okafor - 2024 - {self.TITLE}.pdf": b"%PDF published copy",
            f"appendix - Appendix to {self.TITLE}.pdf": b"%PDF appendix",
        }
        for name, data in files.items():
            (skipped / name).write_bytes(data)
            if name != "same.pdf":  # aliases written by older rules
                pdf2md.add_index_entry(self.output, {"folder": self.NAME, "source_pdf": name,
                                                     "sha256": pdf2md.file_sha256(skipped / name)})
        return skipped

    def verdicts(self):
        return {pdf.name: verdict for pdf, verdict, *_ in pdf2md.recheck_skipped(self.inbox, self.output)}

    def test_verdicts(self):
        self.prepare()
        with mock.patch.object(pdf2md, "pdf_text_and_pages", return_value=(None, None)):
            verdicts = self.verdicts()
        self.assertEqual(verdicts["same.pdf"], "same file")
        self.assertEqual(verdicts["Other - 2020 - Something else entirely about item position effects.pdf"], "wrong")
        self.assertEqual(verdicts[f"Okafor - 2024 - {self.TITLE}.pdf"], "copy")
        self.assertEqual(verdicts[f"appendix - Appendix to {self.TITLE}.pdf"], "wrong")

    def test_long_pdf_against_short_md_is_left_to_check_by_hand(self):
        self.prepare()
        with mock.patch.object(pdf2md, "pdf_text_and_pages", return_value=(f"{self.TITLE} by A. Okafor", 60)):
            self.assertEqual(self.verdicts()[f"Okafor - 2024 - {self.TITLE}.pdf"], "check")

    def test_apply_removes_wrong_entries_and_sends_the_pdfs_back(self):
        skipped = self.prepare()
        args = SimpleNamespace(inbox=self.inbox, output=self.output, apply=True)
        with mock.patch.object(pdf2md, "pdf_text_and_pages", return_value=(None, None)):
            self.assertEqual(pdf2md.cmd_recheck(args, {}), 0)
        self.assertTrue((self.inbox / "Other - 2020 - Something else entirely about item position effects.pdf").exists())
        self.assertTrue((self.inbox / f"appendix - Appendix to {self.TITLE}.pdf").exists())
        self.assertTrue((skipped / f"Okafor - 2024 - {self.TITLE}.pdf").exists())
        self.assertTrue((skipped / "same.pdf").exists())
        sources = [e.get("source_pdf") for e in pdf2md.load_index(self.output)["entries"]]
        self.assertNotIn("Other - 2020 - Something else entirely about item position effects.pdf", sources)
        self.assertIn(f"Okafor - 2024 - {self.TITLE}.pdf", sources)
        self.assertEqual(len(list(self.inbox.glob(f"{pdf2md.INDEX_NAME}.backup-*"))), 1)

    def test_without_apply_nothing_changes(self):
        skipped = self.prepare()
        before = pdf2md.index_path(self.output).read_bytes()
        args = SimpleNamespace(inbox=self.inbox, output=self.output, apply=False)
        with mock.patch.object(pdf2md, "pdf_text_and_pages", return_value=(None, None)):
            self.assertEqual(pdf2md.cmd_recheck(args, {}), 0)
        self.assertEqual(pdf2md.index_path(self.output).read_bytes(), before)
        self.assertEqual(len(list(skipped.glob("*.pdf"))), 4)


if __name__ == "__main__":
    unittest.main()
