# Changelog

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
