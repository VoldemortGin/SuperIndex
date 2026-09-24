# Build the `superindex` executable on an Internet-connected Windows PC.
#
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 -OneFile
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 -Python 3.11
#
# Needs uv (https://docs.astral.sh/uv/). uv downloads Python 3.12 (.python-version)
# when it is missing; -Python is passed to uv as --python (a version or a path).
# Output: dist\superindex\ (or dist\superindex-onefile\) and a zip next to it.
# Compatible with Windows PowerShell 5.1. Keep this file ASCII-only: 5.1 reads
# BOM-less scripts in the ANSI code page.
[CmdletBinding()]
param(
    [switch]$OneFile,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$Dist = Join-Path $Root "dist"
$Work = Join-Path $Root "build"

# Run a native command; stop on a non-zero exit code. stderr is not treated as
# an error (uv and PyInstaller log there).
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @Arguments
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $saved
    }
    if ($code -ne 0) {
        throw "command failed (exit $code): $Exe $($Arguments -join ' ')"
    }
}

# -- 1. uv ---------------------------------------------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv not found. Install it, then open a new PowerShell: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
}
# A separate env with only runtime + build deps (no dev/pdf groups), exactly as uv.lock.
$env:UV_PROJECT_ENVIRONMENT = Join-Path $Root "build\venv-bundle"
$PyArgs = @()
if ($Python) { $PyArgs = @("--python", $Python) }

$longPaths = Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -ErrorAction SilentlyContinue
if (-not $longPaths -or $longPaths.LongPathsEnabled -ne 1) {
    if ($Root.Length -gt 60) {
        Write-Warning "Long paths are disabled and the repo path is long ($($Root.Length) chars); if uv or PyInstaller fails with a path error, move the repo to a short path such as C:\src\SuperIndex."
    }
}

# -- 2. locked dependencies ----------------------------------------------------
Invoke-Native "uv" (@("sync", "--locked", "--no-default-groups", "--group", "build") + $PyArgs)
Invoke-Native "uv" @("run", "--no-sync", "python", "-c", "import struct, sys; print('==> python: %s (%d-bit)' % (sys.version.split()[0], struct.calcsize('P') * 8))")

# -- 3. PyInstaller ------------------------------------------------------------
$AppName = "superindex"
if ($OneFile) { $AppName = "superindex-onefile" }
$App = Join-Path $Dist $AppName
foreach ($p in @((Join-Path $Dist "superindex"), (Join-Path $Dist "superindex.exe"), $App, (Join-Path $Work "superindex"))) {
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}

if ($OneFile) { $env:SUPERINDEX_ONEFILE = "1" } else { $env:SUPERINDEX_ONEFILE = "" }
try {
    Invoke-Native "uv" @("run", "--no-sync", "pyinstaller", (Join-Path $Root "packaging\superindex.spec"),
        "--noconfirm", "--clean", "--distpath", $Dist, "--workpath", $Work)
} finally {
    Remove-Item Env:\SUPERINDEX_ONEFILE -ErrorAction SilentlyContinue
}

if ($OneFile) {
    New-Item -ItemType Directory -Force -Path $App | Out-Null
    Move-Item (Join-Path $Dist "superindex.exe") (Join-Path $App "superindex.exe")
}
$Exe = Join-Path $App "superindex.exe"
if (-not (Test-Path $Exe)) { throw "build produced no $Exe" }

# -- 4. smoke test: fresh cwd, no network, no LLM ------------------------------
$Smoke = Join-Path ([System.IO.Path]::GetTempPath()) ("superindex-smoke-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $Smoke | Out-Null
Copy-Item (Join-Path $Root "samples\aia_ar2021_excerpt.md") $Smoke
$proxyVars = @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
$savedProxy = @{}
foreach ($v in $proxyVars) { $savedProxy[$v] = [Environment]::GetEnvironmentVariable($v, "Process") }
Push-Location $Smoke
try {
    # An unusable proxy makes any hidden download fail loudly.
    foreach ($v in $proxyVars) { [Environment]::SetEnvironmentVariable($v, "http://127.0.0.1:9", "Process") }
    Invoke-Native $Exe @("--help") | Out-Null
    $store = Join-Path $Smoke "store"
    Invoke-Native $Exe @("index", "aia_ar2021_excerpt.md", "--no-summary", "--store", $store)
    if (-not (Test-Path (Join-Path $store "manifest.json"))) { throw "smoke test: no manifest.json in $store" }
} finally {
    Pop-Location
    foreach ($v in $proxyVars) { [Environment]::SetEnvironmentVariable($v, $savedProxy[$v], "Process") }
    Remove-Item -Recurse -Force $Smoke -ErrorAction SilentlyContinue
}
Write-Host "==> smoke test passed"

# -- 5. zip: app folder + .env.example + README --------------------------------
Copy-Item (Join-Path $Root ".env.example") (Join-Path $App ".env.example") -Force
Copy-Item (Join-Path $Root "packaging\README.md") (Join-Path $App "README.md") -Force
$Zip = Join-Path $Dist ($AppName -replace "^superindex", "superindex-windows-x64")
$Zip = "$Zip.zip"
if (Test-Path $Zip) { Remove-Item -Force $Zip }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($App, $Zip, [System.IO.Compression.CompressionLevel]::Optimal, $true)

$appMb = [math]::Round(((Get-ChildItem $App -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
$zipMb = [math]::Round(((Get-Item $Zip).Length / 1MB), 1)
Write-Host "==> $appMb MB  $App"
Write-Host "==> $zipMb MB  $Zip"
