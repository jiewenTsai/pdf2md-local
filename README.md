# pdf2md-local

Turn book and paper PDFs into Markdown on your own computer. Drop a PDF into **input_pdf**, and a few minutes later its Markdown is in **output_md**:
- formulas come out as LaTeX;
- tables keep their structure;
- figures are saved as images next to the text.

```
input_pdf/                       output_md/
  paper.pdf   ──watcher──►         okafor_2021_variational-inference-rating/
  done/paper.pdf                     okafor_2021_variational-inference-rating.md
                                     page_3_image_1.jpg ...
```

It runs [MinerU](https://github.com/opendatalab/MinerU) locally on your GPU, on Windows, macOS (Apple Silicon) and Linux. Nothing is uploaded.

What it never does:
- overwrite an existing md;
- delete a PDF (converted PDFs move to `input_pdf/done/`);
- put a PDF in the output folder;
- run MinerU with `--remote`.

## The main dependency: MinerU, and what it means

pdf2md-local is a thin wrapper. The conversion itself (layout, OCR, formulas, tables) is done entirely by [MinerU](https://github.com/opendatalab/MinerU), which the installer downloads from PyPI. Before installing, know where it comes from:

| | |
|---|---|
| Developer | OpenDataLab, Shanghai Artificial Intelligence Laboratory (上海人工智能实验室), a state-backed research lab in China. |
| Version | Pinned to 4.2.3; the installer never upgrades it. |
| Models | Downloaded on the first conversion from Hugging Face (MinerU's default; ModelScope, run by Alibaba, is the alternative) into `~/.mineru`: the MinerU2.5 vision-language model (1.2B), plus layout and OCR models from Baidu's PaddlePaddle family (PP-DocLayout, PaddleOCR). |
| License | "MinerU Open Source License": Apache 2.0 plus extra terms. A separate commercial license is needed above 100 million monthly users or USD 20 million monthly revenue, and online services built on it must say they use MinerU. Personal and research use on your own computer is not affected. |

What pdf2md-local does about privacy:
- MinerU runs **locally only**. pdf2md-local never passes `--remote`, which would upload the PDF to mineru.net.
- MinerU 4.x contains usage telemetry (to mineru.net, on by default in its `mineru` command). pdf2md-local calls `mineru-kit`, which does not load that code, and in addition starts MinerU with `MINERU_TELEMETRY_DISABLED=1` and `HF_HUB_DISABLE_TELEMETRY=1`.
- During conversion, the only network traffic pdf2md-local itself makes is the metadata lookup described under [Naming](#naming), which you can turn off.

Risks to weigh:
- **Trust in third-party code.** MinerU and its dependencies run with your user's permissions and can read your files, like any program you install. The privacy points above come from reading MinerU 4.2.3's code, not from monitoring its network traffic. If that matters to you, block the converter's network access with a firewall after the models are downloaded, or check the traffic yourself.
- **Origin.** Some employers, universities and government bodies restrict software or AI models developed in China. Check your organisation's policy before installing it on a work computer or using it on confidential documents.
- **Model behaviour.** The models are trained by their developers on data you cannot inspect. For layout analysis and text recognition, the practical risk is recognition errors, not slanted content. Still, check the md against the PDF before you quote it.
- **Supply chain.** The installer installs from PyPI and Hugging Face without checking hashes beyond what pip and Hugging Face do. A compromised upstream release or model would be installed as it is.

## Minimum requirements

pdf2md-local needs a GPU. The installer checks this first and stops before downloading anything if the computer falls short.

| | Requirement |
|---|---|
| GPU (one of) | Discrete NVIDIA/AMD with 6 GB+ video memory (Windows, Linux); Apple Silicon on macOS 14+; integrated Intel Iris Xe / Arc or AMD Radeon 680M–890M (Iris Xe class is the minimum) |
| Memory | 8 GB (discrete GPU, Apple Silicon); 16 GB (integrated GPU, which shares system memory) |
| Disk | 15 GB free |
| Python | 3.11–3.14 for MinerU (the installer finds it) |
| Linux | Vulkan: `sudo apt install libvulkan1 mesa-vulkan-drivers` |

Not supported:
- CPU only;
- Intel UHD 7xx (it works, but at about 142 s per page a 30-page paper takes about 70 minutes);
- Intel HD / UHD 6xx;
- discrete GPUs under 6 GB;
- Intel Macs.

Measured on 6 pages: RTX 3060 took 54 s; Intel UHD 770 took 851 s, with the same output; CPU only would take about 10 hours (stopped early).

`--accept-gpu` lets a GPU pdf2md-local does not recognise through. It never lets a CPU-only machine through.

## Install

Paste one line into a terminal. It downloads this repo into a temp folder, runs the installer, and removes the temp folder again.

**Windows**: open PowerShell (Start menu, type `PowerShell`) and run:

```powershell
irm https://raw.githubusercontent.com/jiewenTsai/pdf2md-local/main/install.ps1 | iex
```

**macOS, Linux**: open Terminal and run:

```sh
curl -fsSL https://raw.githubusercontent.com/jiewenTsai/pdf2md-local/main/install.sh | sh
```

When it finishes, open a **new** terminal window and run `pdf2md-local open` to see the two folders.

To only check the hardware first (installs nothing):

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/jiewenTsai/pdf2md-local/main/install.ps1))) -Check
```

```sh
curl -fsSL https://raw.githubusercontent.com/jiewenTsai/pdf2md-local/main/install.sh | sh -s -- --check
```

The installer takes five steps:
1. Checks the hardware. A computer below the minimum stops here, before anything is downloaded.
2. Finds Python 3.11–3.14.
3. Installs MinerU 4.2.3 into `~/.pdf2md-local/envs/mineru` (a few GB; takes a while). An existing conda env named `pdf2md-mineru` is used as it is, never upgraded.
4. Copies the program to `~/.pdf2md-local/app` and checks that MinerU sees the GPU.
5. Creates `~/pdf2md-local/input_pdf` and `~/pdf2md-local/output_md`, starts the background watcher, and adds the `pdf2md-local` command.

If it stops halfway, fix what it reports and run the same line again; an installed MinerU is not downloaded again.

The first conversion downloads MinerU's models (about 2–3 GB, into `~/.mineru`), so it is slow once.

From a downloaded or cloned copy of this folder, the same installer runs as `sh install.sh` (macOS, Linux) or `powershell -ExecutionPolicy Bypass -File install.ps1` (Windows), with `--check` / `-Check` to only check the hardware.

## Uninstall

**Windows** (PowerShell):

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/jiewenTsai/pdf2md-local/main/install.ps1))) -Uninstall
```

**macOS, Linux** (Terminal):

```sh
curl -fsSL https://raw.githubusercontent.com/jiewenTsai/pdf2md-local/main/install.sh | sh -s -- --uninstall
```

This stops and removes the background watcher, removes the `pdf2md-local` command, and deletes `~/.pdf2md-local` (the program and the MinerU it installed). It works after a half-finished install too.

It does **not** touch:

| What | Where | Remove it yourself if you want |
|---|---|---|
| Your PDFs and md files | `input_pdf`, `output_md` (wherever you moved them) | Delete the folders. |
| MinerU's models (2–3 GB) | `~/.mineru` | Delete the folder, unless another MinerU on this computer still uses it. |
| The config file | listed under [Commands](#commands) | Delete `config.json` and its `pdf2md-local` folder. |
| A conda env `pdf2md-mineru` you had before | your conda folder | `conda env remove -n pdf2md-mineru`, if nothing else uses it. |

## Everyday use

| You want to | Do this |
|---|---|
| Convert a PDF | Drop it into `input_pdf`. |
| Find the folders | `pdf2md-local open` |
| See what is happening | `pdf2md-local status` shows the watcher, the current file with progress, waiting PDFs and the recent log. |
| Convert now, with a progress bar | `pdf2md-local run` |
| Convert a PDF where it is (it is not moved) | `pdf2md-local convert paper.pdf` |
| Ask whether a PDF was converted already | `pdf2md-local check paper.pdf` |
| Check the installation | `pdf2md-local doctor` |

The watcher only converts files that haven't changed for 60 seconds, so a PDF that is still being copied is left alone. It moves each PDF on to one of three folders:
- `done/`: converted.
- `skipped/`: an md already exists.
- `failed/`: see `pdf2md-local_log.txt` in the input folder.

### Checking progress

Open a new terminal window and use any of these:

**Once, right now:**

```sh
pdf2md-local status
```

It shows:
- whether the watcher is running;
- the PDF being converted and how far it is (page and percentage);
- how many PDFs are waiting;
- counts of done, skipped and failed;
- the last lines of the log.

**A live progress bar:**

```sh
pdf2md-local run
```

If the watcher is converting a file, `run` follows its progress. Afterwards it converts whatever else is waiting in `input_pdf`. Ctrl+C stops only the watching; the background watcher carries on.

**The log, as it is written:** each start, finish (with the time it took) and failure is one line.

```sh
tail -f ~/pdf2md-local/input_pdf/pdf2md-local_log.txt
```

```powershell
Get-Content ~\pdf2md-local\input_pdf\pdf2md-local_log.txt -Wait -Tail 20
```

Use your own path if you moved `input_pdf`, and press Ctrl+C to stop.

Normal pauses:
- Before the very first PDF, MinerU downloads its models (2–3 GB), and the progress does not move meanwhile.
- A newly dropped PDF shows as waiting until it has not changed for 60 seconds.

## Moving the folders

You may move or rename `input_pdf` and `output_md` whenever you like, anywhere inside your home folder (Desktop, Documents, a Dropbox folder, …).

Each folder contains a hidden `.pdf2md-local_folder.json` with a random id. **Keep that file.** When a folder is missing, pdf2md-local searches your home folder for that id and follows it:
- the search goes 5 levels deep and skips hidden and system folders;
- it updates the config;
- a running watcher follows within about 15 seconds.

A folder is never recreated at its old path. If it cannot be found (for example, it was moved to another drive), commands say so; run `pdf2md-local setup` to point to it.

## Naming

Each md folder is named `surname_year_short-title`, in lower case, taken from the paper itself:
1. a DOI in the PDF, looked up on Crossref;
2. an arXiv id, looked up on arXiv;
3. the md's title, searched on Crossref;
4. offline: title, first author and year from the md's first page;
5. failing all of that, the file name.

Zotero file names (`Author - 2021 - Title.pdf`) are used as they are.

Only the DOI, arXiv id or title is sent to api.crossref.org / export.arxiv.org; the PDF never leaves the computer. Set `"metadata_lookup": false` in the config to stay offline.

`output_md/pdf2md-local_index.json` maps each PDF's SHA-256 to its folder, so a second copy of the same paper is not converted again.

Each md starts with YAML front matter:
- `source_pdf`, `tool`, `tool_version` and `converted`;
- `title`, `doi`, `named_from` and `source_sha256`.

## Commands

| Command | What it does |
|---|---|
| `setup [--input DIR --output DIR] [--yes]` | Check the hardware and MinerU, create the folders, write the config. |
| `status [--lines N]` | Watcher, current conversion and progress, waiting PDFs, recent log. |
| `open [--print-only]` | Open both folders. |
| `run` | Convert every PDF in the input folder now. |
| `convert PDF...` | Convert PDFs by path; the originals stay where they are. |
| `check PDF...` | `EXISTS`/`MISSING` and the md path. |
| `service install\|uninstall\|restart\|status` | The background watcher. |
| `watch` | Run the watcher in the foreground (the service runs this). |
| `hardware [--require] [--accept-gpu]` | Does this computer meet the minimum requirements? |
| `doctor` | Config, folders, MinerU, GPU, service. |
| `index rebuild [--offline]` | Record existing output folders in the index (nothing is renamed). |

How the watcher is kept running:

| OS | Mechanism |
|---|---|
| Windows | Task Scheduler task `pdf2md-local watcher`: at logon, plus a retry every 5 minutes (ignored while it runs). No console window. |
| macOS | launchd agent `~/Library/LaunchAgents/com.pdf2md-local.watcher.plist` with `KeepAlive`. |
| Linux | systemd user unit `pdf2md-local-watcher.service` with `Restart=always`. Run `loginctl enable-linger $USER` to keep it running while logged out. |

The config file is in one of these places:
- Windows: `%APPDATA%\pdf2md-local\config.json`
- macOS: `~/Library/Application Support/pdf2md-local/config.json`
- Linux: `~/.config/pdf2md-local/config.json`

Overrides, in order of precedence:
1. The `--input`/`--output` flags.
2. The environment variables `PDF2MD_LOCAL_INPUT`/`PDF2MD_LOCAL_OUTPUT`. Folders given this way are used as they are and not followed when moved.
3. The config file.

`PDF2MD_LOCAL_CONFIG` points to another config file.

## Status

Tested on Windows 11 (RTX 3060). The macOS and Linux code paths are there, and unit tests cover the platform logic, but they have not been run on those systems yet.

## Development

```
python -m unittest discover -s tests
```

The tests use only the standard library and run with or without `psutil` and `watchdog`.

## License

pdf2md-local is MIT. MinerU is licensed separately under its own terms; see [the section on MinerU](#the-main-dependency-mineru-and-what-it-means).
