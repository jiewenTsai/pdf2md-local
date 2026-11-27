"""Output names must never change: existing md folders were named by these rules (all lower case)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pdf2md_local as pdf2md  # noqa: E402

# Made-up file names in every shape the rules handle, with the folder names they must get
KNOWN_NAMES = {
    "J. Hollis - 2026 - Explanatory rating models for continuous data A tutorial in R.pdf":
        "hollis_2026_explanatory-rating-models-continuous",
    "Rivera et al. - 2026 - A Gentle Introduction to Bayesian Networks For Teachers and Researchers.pdf":
        "rivera_2026_gentle-introduction-bayesian-networks",
    "Okafor et al. - 2021 - Variational inference for multidimensional rating scal.pdf":
        "okafor_2021_variational-inference-multidimensional-rating",
    "Okafor2021_VIRS.pdf": "okafor2021_virs",
    "Mendes and van der Berg - 2010 - Conditional likelihood inference for a model of item responses.pdf":
        "mendes_2010_conditional-likelihood-inference-model",
    "Ålund …等 - 2026 - Laplace Approximations for Ordinal Data with Nested Random Effects.pdf":
        "ålund_2026_laplace-approximations-ordinal-data",
    "Example-et-al.-2004-Four-Part-Framework-for-Classroom-Assessment.pdf":
        "example-et-al.-2004-four-part-framework-for-classroom-assessment",
    "Haddad et al. - 2025 - Robust standard errors for bayesian median regression with grouped data.pdf":
        "haddad_2025_robust-standard-errors-bayesian",
    "Ålund 等 - 2023 - Quick, Simple Approximations to Posterior Distributions in Mixed Models.pdf":
        "ålund_2023_quick-simple-approximations-posterior",
    "Paper.pdf": "paper",
    "Nakamura - 2015 - Score-based tests for latent class analysis.pdf": "nakamura_2015_score-based-tests-latent",
    "Lee - 1991 - A three-parameter graded response model.pdf": "lee_1991_three-parameter-graded-response",
    "smith2015.pdf.pdf": "smith2015.pdf",
    "某大學的「課程滿意度問卷」分數比較：多層次模型分析-1.pdf":
        "某大學的「課程滿意度問卷」分數比較：多層次模型分析-1",
}

# Author spellings from Zotero in English and Chinese UIs
AUTHOR_VARIANTS = {
    "Ålund …等": "Ålund",
    "Ålund ...等": "Ålund",
    "Ålund 等": "Ålund",
    "Ålund等人": "Ålund",
    "Mendes 和 Okafor": "Mendes",
    "Mendes 與 Okafor": "Mendes",
    "Mendes and Okafor": "Mendes",
    "Mendes & Okafor": "Mendes",
    "Rivera et al.": "Rivera",
    "J. Hollis": "Hollis",
    "J.K. Hollis": "Hollis",
    "J.-K. Hollis": "Hollis",
    "A. B. Smith and Lee": "Smith",
    "van der Berg": "van-der-Berg",
}


class NamingTest(unittest.TestCase):
    def test_known_pdfs_keep_their_folder_names(self):
        for pdf_name, expected in KNOWN_NAMES.items():
            with self.subTest(pdf=pdf_name):
                self.assertEqual(pdf2md.output_name_for(Path(pdf_name)), expected.lower())

    def test_author_variants(self):
        for author, expected in AUTHOR_VARIANTS.items():
            with self.subTest(author=author):
                self.assertEqual(pdf2md.first_author_surname(author), expected)

    def test_short_title_needs_three_words(self):
        self.assertIsNone(pdf2md.short_title("On the IRT"))
        self.assertEqual(pdf2md.output_name_for(Path("Lord - 1980 - On IRT.pdf")), "lord_-_1980_-_on_irt")

    def test_names_are_lower_case(self):
        self.assertEqual(pdf2md.output_name_for(Path("Okafor et al. - 2021 - Variational inference for multidimensional rating scal.pdf")),
                         "okafor_2021_variational-inference-multidimensional-rating")

    def test_md_path(self):
        path = pdf2md.md_path_for("random notes v2.pdf", "/out")
        self.assertEqual(path, Path("/out/random_notes_v2/random_notes_v2.md"))


if __name__ == "__main__":
    unittest.main()
