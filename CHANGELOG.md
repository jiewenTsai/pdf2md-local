# Changelog

## 1.2.0 (2026-11-27)

- **New command `recheck [--apply]`.** It finds PDFs in `input_pdf/skipped/` that versions up to 1.1.0 skipped by mistake. Each PDF is shown as `same file`, `copy`, `MISTAKE` or `CHECK`. The folder's title is looked for on the PDF's first page (with pypdfium2, which comes with MinerU) or in its file name. `--apply` backs up the index, removes the wrong entries and sends those PDFs back to be converted. Without `--apply`, nothing changes.

## 1.1.2 (2026-11-27)

- Appendices, supplements and errata are documents of their own. An md whose own title starts with "Appendix", "Supplementary", "Supporting information", "Erratum", "Corrigendum" or "Correction to" is saved as `<paper name>_appendix` (or `_supplement`, `_erratum`), never skipped as its paper. Its index entry has no DOI, so a DOI lookup still finds the paper. Before, Crossref gave an appendix its paper's DOI, and the appendix was skipped as the paper.

## 1.1.1 (2026-11-27)

Fixes to how an already converted paper is recognised. They come from the same fixes in pdf2md, where a real collection of 106 papers showed them.
- A DOI in a link (such as `doi.org/10....` in the reference list) no longer counts as the paper's own DOI. Before, a paper that cited an already converted paper was skipped as that paper, and the mistake was remembered in the index.
- A metadata DOI is checked against the title in the **same** metadata block (XMP packet or document info). Titles of embedded figures and software-made titles ("Microsoft Word - x.docx", "R Graphics Output") are ignored. Document-info strings are decoded properly, and the end of large files is read too.
- A DOI from a link counts when one of the PDF's titles matches the indexed title.
- After conversion, the same DOI or title with one md more than 3 times as long as the other is kept as a separate document (a summary, supplement or erratum). When several mds share a DOI, the longest one is found.
- Initials before the first author's surname in Zotero file names are dropped ("J. Hollis - 2026 - ..." becomes `hollis_2026_...`).

## 1.1.0 (2026-11-25)

- One-line install and uninstall: `irm … | iex` on Windows, `curl … | sh` on macOS and Linux.
- The installer's second hardware check (after MinerU is installed) requires 5 GB free instead of 15, which had refused a good install on a Mac with 13 GB left.
- MinerU always runs with its telemetry and Hugging Face Hub telemetry turned off.
- pip installs leave no download cache behind.
- README: separate Install and Uninstall sections, and a section on MinerU's origin, license and risks.

## 1.0.0 (2026-11-24)

First release. pdf2md-local is a standalone tool split off from pdf2md 3.4.0: it converts PDFs to Markdown on the computer it is installed on, and nothing else.

Kept from pdf2md:
- the MinerU conversion with progress;
- the naming rules (DOI, arXiv, title, lower case) and the SHA-256 index;
- the hardware minimum (GPU required; Iris Xe class or better for integrated GPUs);
- folders that can be moved or renamed;
- the background watcher service.

Removed:
- the client role and handing PDFs to another machine;
- sync-folder detection;
- the Claude skill (`skill`, `deploy`, SKILL.md);
- the desktop shortcut;
- Marker as a second tool;
- `--allow-cpu`.

Changed:
- Everything has its own name, so it can be installed next to pdf2md:
  - command, config folder and cache: `pdf2md-local`;
  - service: `pdf2md-local watcher`;
  - install folder: `~/.pdf2md-local`;
  - marker: `.pdf2md-local_folder.json`;
  - log: `pdf2md-local_log.txt`;
  - index: `pdf2md-local_index.json`;
  - environment variables: `PDF2MD_LOCAL_*`.
- Default folders: `~/pdf2md-local/input_pdf` and `~/pdf2md-local/output_md`.
- Moved folders are searched for in the home folder.
- `inbox` is now `run`; the flag `--inbox` is now `--input`.
- `setup` refuses before writing anything when the computer is below the minimum or MinerU sees only the CPU, and when the input and output folders are the same or nested.
- New: the `open` command, and `--uninstall` in the installers.
