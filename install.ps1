# pdf2md-local installer for Windows.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1              check, install MinerU, create the folders, start the watcher
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Check       only the hardware check (installs nothing)
#   powershell -ExecutionPolicy Bypass -File install.ps1 -AcceptGpu   treat an unrecognised GPU as usable (never CPU-only)
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall   stop the watcher and remove the program (folders stay)
#
# A computer below the minimum requirements stops at step 1, before anything is downloaded.
#
# One line, without downloading anything first:
#   irm https://raw.githubusercontent.com/jiewenTsai/pdf2md-local/main/install.ps1 | iex
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/jiewenTsai/pdf2md-local/main/install.ps1))) -Check
param([switch]$Check, [switch]$AcceptGpu, [switch]$Uninstall)

# Run through irm | iex there is no script file: fetch the repo into a temp folder and run its
# install.ps1 in a child PowerShell, so "exit" there never closes the user's window.
if (-not $MyInvocation.MyCommand.Path) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("pdf2md-local-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force $tmp | Out-Null
    Write-Host "Downloading pdf2md-local ..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing "https://github.com/jiewenTsai/pdf2md-local/archive/refs/heads/main.zip" -OutFile "$tmp\src.zip"
    Expand-Archive "$tmp\src.zip" -DestinationPath $tmp
    $childArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "$tmp\pdf2md-local-main\install.ps1")
    if ($Check) { $childArgs += "-Check" }
    if ($AcceptGpu) { $childArgs += "-AcceptGpu" }
    if ($Uninstall) { $childArgs += "-Uninstall" }
    & powershell.exe @childArgs
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    return
}

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$HomeDir = Join-Path $env:USERPROFILE ".pdf2md-local"
$App = Join-Path $HomeDir "app"
$EnvDir = Join-Path $HomeDir "envs\mineru"
$BinDir = Join-Path $HomeDir "bin"
$MineruVersion = "4.2.3"
$Accept = @()
if ($AcceptGpu) { $Accept = @("--accept-gpu") }

function Test-Python($exe, $versionArgs, $test) {
    try {
        & $exe @versionArgs -c $test 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

# Python 3.8+ to run the checks ("python" may be a dead Microsoft Store alias)
$Py = $null; $PyArgs = @()
$candidates = @(@("py", @("-3")), @("python", @()), @("python3", @()))
foreach ($base in @("miniconda3", "anaconda3", "miniforge3")) {
    $candidates += ,@((Join-Path $env:USERPROFILE "$base\python.exe"), @())
}
foreach ($c in $candidates) {
    if ((Get-Command $c[0] -ErrorAction SilentlyContinue) -or (Test-Path $c[0])) {
        if (Test-Python $c[0] $c[1] "import sys; sys.exit(sys.version_info < (3, 8))") { $Py = $c[0]; $PyArgs = $c[1]; break }
    }
}
if (-not $Py) { Write-Host "Python 3.8+ not found. Install it from python.org (tick 'Add to PATH')."; exit 1 }

if ($Uninstall) {
    if (Test-Path "$App\pdf2md_local.py") { & $Py @PyArgs -X utf8 "$App\pdf2md_local.py" service uninstall }
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -and ($userPath.Split(";") -contains $BinDir)) {
        [Environment]::SetEnvironmentVariable("Path", (($userPath.Split(";") | Where-Object { $_ -ne $BinDir }) -join ";"), "User")
    }
    if (Test-Path $HomeDir) { Remove-Item -Recurse -Force $HomeDir }
    Write-Host "Removed the watcher and $HomeDir."
    Write-Host "Your input_pdf and output_md folders and everything in them are untouched."
    exit 0
}

Write-Host "== 1/5 Hardware check"
& $Py @PyArgs -X utf8 "$Here\pdf2md_local.py" hardware --require @Accept
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "This computer does not meet the minimum requirements (see README). Nothing was installed."
    exit 1
}
if ($Check) { Write-Host "Hardware OK (-Check: nothing installed)."; exit 0 }

# An existing MinerU (this installer's, or a conda env named pdf2md-mineru) is used as it is, never upgraded
$Existing = & $Py @PyArgs -X utf8 -c "import sys; sys.path.insert(0, sys.argv[1]); import pdf2md_local as p; t = p.detect_tool('mineru'); print(t['python'] if t else '')" $Here

Write-Host "== 2/5 Python 3.11-3.14 for MinerU"
$EnvPy = $null; $EnvPyArgs = @()
if ($Existing) {
    Write-Host "MinerU already installed (left as it is): $Existing"
} else {
    foreach ($v in @("3.13", "3.12", "3.11", "3.14")) {
        if ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-Python "py" @("-$v") "import sys")) { $EnvPy = "py"; $EnvPyArgs = @("-$v"); break }
    }
    if (-not $EnvPy) { Write-Host "MinerU needs Python 3.11-3.14. Install Python 3.12 from python.org, then run this again."; exit 1 }
    Write-Host "using py $EnvPyArgs"
}

Write-Host "== 3/5 MinerU $MineruVersion (several GB; takes a while)"
if ($Existing) {
    $RunPy = $Existing
    Write-Host "skipped"
} else {
    & $EnvPy @EnvPyArgs -m venv $EnvDir
    & "$EnvDir\Scripts\python.exe" -m pip install --no-cache-dir --upgrade pip
    & "$EnvDir\Scripts\python.exe" -m pip install --no-cache-dir "mineru==$MineruVersion" psutil watchdog
    if ($LASTEXITCODE -ne 0) { Write-Host "pip install failed; see above."; exit 1 }
    $RunPy = "$EnvDir\Scripts\python.exe"
}

Write-Host "== 4/5 Does MinerU see the GPU?"
New-Item -ItemType Directory -Force $App | Out-Null
foreach ($f in @("pdf2md_local.py", "README.md", "README_zh-TW.md")) {
    if (Test-Path "$Here\$f") { Copy-Item "$Here\$f" "$App\$f" -Force }
}
& $RunPy -X utf8 "$App\pdf2md_local.py" hardware --require @Accept
if ($LASTEXITCODE -ne 0) { Write-Host "MinerU is installed but cannot use the GPU; see the problem above."; exit 1 }

Write-Host "== 5/5 Folders and the background watcher"
& $RunPy -X utf8 "$App\pdf2md_local.py" setup @Accept
if ($LASTEXITCODE -ne 0) { exit 1 }
& $RunPy -X utf8 "$App\pdf2md_local.py" service install
if ($LASTEXITCODE -ne 0) { exit 1 }
New-Item -ItemType Directory -Force $BinDir | Out-Null
Set-Content -Encoding ascii -Path "$BinDir\pdf2md-local.cmd" -Value "@`"$RunPy`" -X utf8 `"$App\pdf2md_local.py`" %*"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath -or -not ($userPath.Split(";") -contains $BinDir)) {
    [Environment]::SetEnvironmentVariable("Path", ((@($userPath, $BinDir) | Where-Object { $_ }) -join ";"), "User")
}
Write-Host ""
Write-Host "Done. Drop PDFs into the input folder; the md files appear in the output folder."
Write-Host "The first conversion downloads MinerU's models (a few GB), so it is slow once."
Write-Host "Command (in a new terminal): pdf2md-local status, pdf2md-local open"
