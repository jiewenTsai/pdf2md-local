#!/bin/sh
# pdf2md-local installer for macOS (Apple Silicon) and Linux (Ubuntu).
#
#   sh install.sh               check the hardware, install MinerU, create the folders, start the watcher
#   sh install.sh --check       only the hardware check (installs nothing)
#   sh install.sh --accept-gpu  treat a GPU pdf2md-local does not recognise as usable (never CPU-only)
#   sh install.sh --uninstall   stop the watcher and remove the program (your folders and files stay)
#
# A computer below the minimum requirements stops at step 1, before anything is downloaded.
#
# One line, without downloading anything first:
#   curl -fsSL https://raw.githubusercontent.com/jiewenTsai/pdf2md-local/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/jiewenTsai/pdf2md-local/main/install.sh | sh -s -- --check
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"

# Piped into sh there is no checkout next to this script: fetch the repo into a temp folder and run its install.sh
if [ ! -f "$HERE/pdf2md_local.py" ]; then
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    echo "Downloading pdf2md-local ..."
    curl -fsSL "https://github.com/jiewenTsai/pdf2md-local/archive/refs/heads/main.tar.gz" | tar xz -C "$TMP"
    sh "$TMP/pdf2md-local-main/install.sh" "$@"
    exit $?
fi
HOME_DIR="$HOME/.pdf2md-local"
APP="$HOME_DIR/app"
ENV="$HOME_DIR/envs/mineru"
LAUNCHER="$HOME/.local/bin/pdf2md-local"
MINERU_VERSION="4.2.3"
MODE=install
ACCEPT=""
for arg in "$@"; do
    case "$arg" in
        --check) MODE=check ;;
        --uninstall) MODE=uninstall ;;
        --accept-gpu) ACCEPT="--accept-gpu" ;;
        *) echo "unknown option: $arg"; exit 2 ;;
    esac
done

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then
        PY="$candidate"; break
    fi
done
if [ -z "$PY" ]; then
    echo "Python 3.8+ not found. macOS: xcode-select --install ; Ubuntu: sudo apt install python3"
    exit 1
fi

if [ "$MODE" = uninstall ]; then
    if [ -f "$APP/pdf2md_local.py" ]; then "$PY" "$APP/pdf2md_local.py" service uninstall || true; fi
    rm -f "$LAUNCHER"
    rm -rf "$HOME_DIR"
    echo "Removed the watcher, $HOME_DIR and $LAUNCHER."
    echo "Your input_pdf and output_md folders and everything in them are untouched."
    exit 0
fi

echo "== 1/5 Hardware check"
if ! "$PY" "$HERE/pdf2md_local.py" hardware --require $ACCEPT; then
    echo
    echo "This computer does not meet the minimum requirements (see README). Nothing was installed."
    exit 1
fi
if [ "$MODE" = check ]; then echo "Hardware OK (--check: nothing installed)."; exit 0; fi

if [ "$(uname -s)" = "Linux" ]; then
    # MinerU's VLM runs on Intel/AMD/NVIDIA GPUs through Vulkan
    if ! { ldconfig -p 2>/dev/null | grep -q 'libvulkan\.so\.1'; }; then
        echo "The Vulkan loader is missing. Install it, then run this again:"
        echo "  sudo apt install libvulkan1 mesa-vulkan-drivers"
        exit 1
    fi
fi

# An existing MinerU (this installer's, or a conda env named pdf2md-mineru) is used as it is, never upgraded
EXISTING="$("$PY" -c "import sys; sys.path.insert(0, sys.argv[1]); import pdf2md_local as p; t = p.detect_tool('mineru'); print(t['python'] if t else '')" "$HERE")"

echo "== 2/5 Python 3.11-3.14 for MinerU"
ENV_PY=""
if [ -n "$EXISTING" ]; then
    echo "MinerU already installed (left as it is): $EXISTING"
else
    for candidate in python3.13 python3.12 python3.11 python3.14 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(not ((3, 11) <= sys.version_info[:2] <= (3, 14)))' 2>/dev/null; then
            ENV_PY="$candidate"; break
        fi
    done
    if [ -z "$ENV_PY" ]; then
        echo "MinerU needs Python 3.11-3.14. Ubuntu 24.04 has 3.12; on older systems install one, e.g.:"
        echo "  sudo apt install python3.12 python3.12-venv     (or use Miniconda / uv)"
        exit 1
    fi
    echo "using $ENV_PY ($("$ENV_PY" --version))"
fi

echo "== 3/5 MinerU $MINERU_VERSION (several GB; takes a while)"
if [ -n "$EXISTING" ]; then
    RUN_PY="$EXISTING"
    echo "skipped"
else
    "$ENV_PY" -m venv "$ENV"
    "$ENV/bin/python" -m pip install --upgrade pip
    "$ENV/bin/python" -m pip install "mineru==$MINERU_VERSION" psutil watchdog
    RUN_PY="$ENV/bin/python"
fi

echo "== 4/5 Does MinerU see the GPU?"
mkdir -p "$APP"
for file in pdf2md_local.py README.md README_zh-TW.md; do
    if [ -f "$HERE/$file" ]; then cp "$HERE/$file" "$APP/$file"; fi
done
if ! "$RUN_PY" "$APP/pdf2md_local.py" hardware --require $ACCEPT; then
    echo "MinerU is installed but cannot use the GPU; see the problem above."
    exit 1
fi

echo "== 5/5 Folders and the background watcher"
"$RUN_PY" "$APP/pdf2md_local.py" setup $ACCEPT
"$RUN_PY" "$APP/pdf2md_local.py" service install
mkdir -p "$(dirname "$LAUNCHER")"
printf '#!/bin/sh\nexec "%s" "%s" "$@"\n' "$RUN_PY" "$APP/pdf2md_local.py" > "$LAUNCHER"
chmod +x "$LAUNCHER"
echo
echo "Done. Drop PDFs into the input folder; the md files appear in the output folder."
echo "The first conversion downloads MinerU's models (a few GB), so it is slow once."
echo "Command: $LAUNCHER  (try: pdf2md-local status, pdf2md-local open)"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "Note: $HOME/.local/bin is not on your PATH; open a new terminal, or run the command by its full path." ;;
esac
