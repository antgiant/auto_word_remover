#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Voice_to_Text launcher - high-quality local speech-to-text.

.DESCRIPTION
  Thin wrapper around transcribe.py. Runs the dedicated venv. All arguments
  are passed straight through.

.EXAMPLE
  .\stt.ps1 "C:\path\meeting.m4a"
  .\stt.ps1 "C:\clips" --model large-v2 --max-speakers 3
  .\stt.ps1 "call.mp4" --no-diarize --emit txt
  .\stt.ps1 -Help
#>
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Passthru
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $py)) {
    Write-Error "venv not found at $py  - run setup again (see README.md)"
    exit 1
}

$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if (-not $Passthru -or $Passthru.Count -eq 0 -or $Passthru -contains '-Help' -or $Passthru -contains '--help' -or $Passthru -contains '-h') {
    & $py (Join-Path $root 'transcribe.py') --help
    exit $LASTEXITCODE
}

& $py -X utf8 (Join-Path $root 'transcribe.py') @Passthru
exit $LASTEXITCODE
