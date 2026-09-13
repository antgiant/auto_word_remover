#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Profanity_Filter launcher - bleep flagged profanity in a media file.
.DESCRIPTION
  Thin wrapper around clean.py. Runs it with the sibling voice_to_text
  project's venv (Python 3.11, already has everything) and forces UTF-8
  console output.
.EXAMPLE
  .\clean.ps1 "C:\Media\Movie (2002).mkv"
  .\clean.ps1 "Movie.mkv" --dry-run
  .\clean.ps1 "Movie.mkv" --pad 0.15 --beep-gain-db -8 --keep-temp
#>
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Passthru
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$py = Join-Path $root '..\voice_to_text\.venv\Scripts\python.exe'

if (-not (Test-Path $py)) {
    Write-Error "voice_to_text venv not found at $py - set that project up first (see its README.md)"
    exit 1
}

$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if (-not $Passthru -or $Passthru.Count -eq 0 -or $Passthru -contains '-h' -or $Passthru -contains '--help') {
    & $py -X utf8 (Join-Path $root 'clean.py') --help
    exit $LASTEXITCODE
}

& $py -X utf8 (Join-Path $root 'clean.py') @Passthru
exit $LASTEXITCODE
