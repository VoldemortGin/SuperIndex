# Index Markdown (unchanged files are skipped) and run a question set.
#
#   powershell -ExecutionPolicy Bypass -File scripts\run_batch.ps1          # the two sample .md + sample questions
#   powershell -ExecutionPolicy Bypass -File scripts\run_batch.ps1 -Markdown D:\corpus_md -Questions D:\q.jsonl
#   powershell -ExecutionPolicy Bypass -File scripts\run_batch.ps1 -Summary -Store D:\store -Extra "--concurrency","2"
#
# -Summary builds the index with LLM node summaries (slower); default is --no-summary.
# Output: results\batch\<timestamp>\summary.md and results.jsonl.
# Compatible with Windows PowerShell 5.1. Keep this file ASCII-only.
[CmdletBinding()]
param(
    [string[]]$Markdown = @("samples\aia_ar2021_excerpt.md", "samples\di_native_excerpt.md"),
    [string]$Questions = "samples\questions_sample.jsonl",
    [string]$Store = "",
    [switch]$Summary,
    [string[]]$Extra = @()
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv not found. Install it, then open a new PowerShell: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
}
$Cli = @("run", "scripts/superindex.py")

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

$env:PYTHONUTF8 = "1"
$StoreArgs = @()
if ($Store) { $StoreArgs = @("--store", $Store) }

foreach ($Md in $Markdown) {
    $IndexArgs = $Cli + @("index", $Md) + $StoreArgs
    if (-not $Summary) { $IndexArgs += "--no-summary" }
    Write-Host "== index $Md"
    Invoke-Native "uv" $IndexArgs
}

$Out = Join-Path $Root ("results\batch\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
Write-Host "== batch $Questions"
Invoke-Native "uv" ($Cli + @("batch", $Questions, "--out", $Out) + $StoreArgs + $Extra)

Write-Host ""
Write-Host "summary: $(Join-Path $Out 'summary.md')"
