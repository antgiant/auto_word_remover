#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Sets up this toolkit's requirements on Windows: Python venv + WhisperX for
  voice_to_text, ffmpeg, and MKVToolNix for profanity_filter.

.DESCRIPTION
  gpu_lock needs nothing (stdlib only). This script handles the two pieces
  that do:
    - voice_to_text: Python 3.11+, a dedicated .venv, torch/WhisperX
    - profanity_filter: ffmpeg/ffprobe on PATH, MKVToolNix (mkvmerge/mkvextract)

  Missing system tools (Python, ffmpeg, MKVToolNix) are installed via winget
  when possible; otherwise this prints the manual step and keeps going.

  Three optional extras are each several GB and off by default - pass the
  matching switch to include one:
    -IncludeStemmer   ambient-noise-preserving mute / --method dialog
    -IncludeTts       --method replace (word swap instead of removal)
    -IncludeOcr       Blu-ray/DVD bitmap subtitle OCR (Tesseract)

  An OpenSubtitles API key and a TMDB API key (both free, both optional) need
  a personal account, so they can't be scripted - see profanity_filter's own
  README.md for those two.

.PARAMETER IncludeStemmer
  Also set up profanity_filter\.venv-stem (audio-separator) for
  mute_fill = "stems" and --method dialog.

.PARAMETER IncludeTts
  Also set up profanity_filter\.venv-tts (Piper + RVC) and download its two
  default voice models, for --method replace.

.PARAMETER IncludeOcr
  Also install Tesseract OCR (winget) and pytesseract, for PGS/VobSub bitmap
  subtitle OCR.

.PARAMETER Cpu
  Install CPU-only builds of torch/audio-separator instead of CUDA ones.
  Smaller download; everything still works, just slower. Use this if the
  machine has no NVIDIA GPU.

.PARAMETER Force
  Rebuild a venv that's already set up instead of skipping it.

.PARAMETER SkipWinget
  Never shell out to winget - just report what's missing and how to install
  it by hand.

.EXAMPLE
  .\install.ps1
  .\install.ps1 -IncludeStemmer -IncludeOcr
  .\install.ps1 -Cpu
  .\install.ps1 -Force
#>
param(
    [switch]$IncludeStemmer,
    [switch]$IncludeTts,
    [switch]$IncludeOcr,
    [switch]$Cpu,
    [switch]$Force,
    [switch]$SkipWinget
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$warnings = [System.Collections.Generic.List[string]]::new()

function Write-Step($msg) {
    Write-Host ""
    Write-Host "== $msg ==" -ForegroundColor Cyan
}

function Write-Warn($msg) {
    Write-Host "  ! $msg" -ForegroundColor Yellow
    $warnings.Add($msg)
}

function Test-CommandExists($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Install-WithWinget($id, $friendlyName) {
    if ($SkipWinget) {
        Write-Warn "$friendlyName not found - -SkipWinget was passed, install it manually."
        return $false
    }
    if (-not (Test-CommandExists 'winget')) {
        Write-Warn "$friendlyName not found and winget isn't available - install it manually."
        return $false
    }
    Write-Host "  Installing $friendlyName via winget ($id)..."
    winget install --id $id --exact --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "winget install of $friendlyName exited with code $LASTEXITCODE - install it manually."
        return $false
    }
    return $true
}

# ---------------------------------------------------------------------------
Write-Step "Python 3.11+"

$PythonLauncher = $null
$PythonLauncherArgs = @()

if (Test-CommandExists 'py') {
    & py -3.11 -c "exit(0)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $PythonLauncher = 'py'
        $PythonLauncherArgs = @('-3.11')
    }
}
if (-not $PythonLauncher -and (Test-CommandExists 'python')) {
    $verOut = & python -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
    if ($LASTEXITCODE -eq 0 -and $verOut) {
        $parts = $verOut.Trim().Split('.')
        if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11) {
            $PythonLauncher = 'python'
        }
    }
}

if (-not $PythonLauncher) {
    Write-Host "  Not found."
    if (Install-WithWinget 'Python.Python.3.11' 'Python 3.11') {
        Write-Host ""
        Write-Host "Python 3.11 was just installed - open a NEW terminal (PATH needs to refresh)" -ForegroundColor Yellow
        Write-Host "and run .\install.ps1 again to continue." -ForegroundColor Yellow
    } else {
        Write-Host ""
        Write-Host "Install Python 3.11+ yourself (https://www.python.org/downloads/) and re-run this script." -ForegroundColor Yellow
    }
    exit 1
}
Write-Host "  Using: $PythonLauncher $($PythonLauncherArgs -join ' ')"

function Invoke-Python {
    param([Parameter(Mandatory)][string[]]$ArgList)
    & $PythonLauncher @PythonLauncherArgs @ArgList
}

# ---------------------------------------------------------------------------
Write-Step "ffmpeg / ffprobe"

if ((Test-CommandExists 'ffmpeg') -and (Test-CommandExists 'ffprobe')) {
    Write-Host "  Found on PATH."
} else {
    Write-Host "  Not found on PATH."
    if (Install-WithWinget 'Gyan.FFmpeg' 'ffmpeg') {
        Write-Warn "ffmpeg was just installed - open a NEW terminal so PATH updates take effect before using it."
    } else {
        Write-Warn "Install ffmpeg yourself and put ffmpeg.exe/ffprobe.exe on PATH (https://www.gyan.dev/ffmpeg/builds/)."
    }
}

# ---------------------------------------------------------------------------
Write-Step "voice_to_text: venv + WhisperX"

$vtt = Join-Path $root 'voice_to_text'
$venv = Join-Path $vtt '.venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'

$needsSetup = $Force -or -not (Test-Path $venvPy)
if (-not $needsSetup) {
    & $venvPy -c "import whisperx" 2>$null
    if ($LASTEXITCODE -ne 0) { $needsSetup = $true }
}

if (-not $needsSetup) {
    Write-Host "  Already set up ($venvPy) - pass -Force to rebuild."
} else {
    Push-Location $vtt
    try {
        if (Test-Path $venv) { Remove-Item $venv -Recurse -Force }

        Write-Host "  Creating venv..."
        Invoke-Python -ArgList @('-m', 'venv', '.venv')
        if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }

        & $venvPy -m pip install --upgrade pip | Out-Null

        if ($Cpu) {
            $torchPkgs = @('torch==2.8.0', 'torchaudio==2.8.0', 'torchvision==0.23.0')
            $torchIndex = @()
        } else {
            $torchPkgs = @('torch==2.8.0+cu128', 'torchaudio==2.8.0+cu128', 'torchvision==0.23.0+cu128')
            $torchIndex = @('--index-url', 'https://download.pytorch.org/whl/cu128')
        }

        Write-Host "  Installing torch ($(if ($Cpu) { 'CPU-only' } else { 'CUDA 12.8' }))..."
        & $venvPy -m pip install @torchPkgs @torchIndex
        if ($LASTEXITCODE -ne 0) { throw "torch install failed" }

        Write-Host "  Installing whisperx..."
        & $venvPy -m pip install whisperx hf_xet soundfile
        if ($LASTEXITCODE -ne 0) { throw "whisperx install failed" }

        Write-Host "  Re-pinning torch (installing whisperx pulls in the CPU build otherwise)..."
        & $venvPy -m pip install --force-reinstall @torchPkgs @torchIndex
        if ($LASTEXITCODE -ne 0) { throw "torch re-pin failed" }

        Write-Host "  Done."
    } catch {
        Write-Warn "voice_to_text venv setup failed: $($_.Exception.Message)"
    } finally {
        Pop-Location
    }
}

if ((Test-Path $venvPy) -and -not $Cpu) {
    $cuda = & $venvPy -c "import torch; print(torch.cuda.is_available())" 2>$null
    Write-Host "  torch.cuda.is_available() -> $cuda"
    if ($cuda -ne 'True') {
        Write-Warn "CUDA isn't available to torch - voice_to_text will fall back to (slower) CPU. Check your NVIDIA driver if you expected GPU support."
    }
}

# ---------------------------------------------------------------------------
Write-Step "MKVToolNix (mkvmerge / mkvextract)"

$pf = Join-Path $root 'profanity_filter'
$mkvFound = (Test-Path (Join-Path $pf 'bin\mkvtoolnix\mkvmerge.exe')) `
    -or (Test-Path (Join-Path $pf 'bin\mkvmerge.exe')) `
    -or (Test-CommandExists 'mkvmerge') `
    -or (Test-Path 'C:\Program Files\MKVToolNix\mkvmerge.exe')

if ($mkvFound) {
    Write-Host "  Found."
} else {
    Write-Host "  Not found."
    if (Install-WithWinget 'MoritzBunkus.MKVToolNix' 'MKVToolNix') {
        Write-Host "  Installed system-wide - clean.py auto-detects it under Program Files, no PATH change needed."
    } else {
        Write-Warn "Install MKVToolNix yourself: https://mkvtoolnix.download/downloads.html#windows (portable build into profanity_filter\bin\mkvtoolnix\, or the regular installer)."
    }
}

# ---------------------------------------------------------------------------
if ($IncludeOcr) {
    Write-Step "Tesseract OCR (optional: Blu-ray/DVD bitmap subtitle OCR)"

    if (Test-CommandExists 'tesseract') {
        Write-Host "  Found."
    } elseif (Install-WithWinget 'UB-Mannheim.TesseractOCR' 'Tesseract OCR') {
        Write-Warn "Tesseract was just installed - open a NEW terminal before relying on PGS/VobSub OCR."
    } else {
        Write-Warn "Install Tesseract yourself: winget install --id UB-Mannheim.TesseractOCR"
    }

    if (Test-Path $venvPy) {
        Write-Host "  Installing pytesseract into voice_to_text's venv..."
        & $venvPy -m pip install pytesseract
    }
}

# ---------------------------------------------------------------------------
if ($IncludeStemmer) {
    Write-Step "profanity_filter: stemmer venv (.venv-stem) - audio-separator"

    $stemVenv = Join-Path $pf '.venv-stem'
    $stemPy = Join-Path $stemVenv 'Scripts\python.exe'

    if ((Test-Path $stemPy) -and -not $Force) {
        Write-Host "  Already set up - pass -Force to rebuild."
    } else {
        Push-Location $pf
        try {
            if (Test-Path $stemVenv) { Remove-Item $stemVenv -Recurse -Force }

            Invoke-Python -ArgList @('-m', 'venv', '.venv-stem')
            if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }

            & $stemPy -m pip install --upgrade pip | Out-Null

            $extra = if ($Cpu) { 'audio-separator[cpu]' } else { 'audio-separator[gpu]' }
            Write-Host "  Installing $extra..."
            & $stemPy -m pip install $extra
            if ($LASTEXITCODE -ne 0) { throw "audio-separator install failed" }

            if (-not $Cpu) {
                Write-Host "  Installing CUDA torch + onnxruntime runtime DLLs..."
                & $stemPy -m pip install "torch==2.6.0+cu124" "torchvision==0.21.0+cu124" --index-url https://download.pytorch.org/whl/cu124
                if ($LASTEXITCODE -ne 0) { throw "torch (cu124) install failed" }

                & $stemPy -m pip install nvidia-cudnn-cu12 nvidia-cublas-cu12 nvidia-cuda-runtime-cu12 nvidia-cufft-cu12
                if ($LASTEXITCODE -ne 0) { throw "nvidia runtime packages failed" }
            }

            Write-Host "  Done."
        } catch {
            Write-Warn ".venv-stem setup failed: $($_.Exception.Message)"
        } finally {
            Pop-Location
        }
    }
}

# ---------------------------------------------------------------------------
if ($IncludeTts) {
    Write-Step "profanity_filter: TTS/voice-conversion venv (.venv-tts) - for --method replace"

    $ttsVenv = Join-Path $pf '.venv-tts'
    $ttsPy = Join-Path $ttsVenv 'Scripts\python.exe'

    if ((Test-Path $ttsPy) -and -not $Force) {
        Write-Host "  Already set up - pass -Force to rebuild."
    } else {
        Push-Location $pf
        try {
            if (Test-Path $ttsVenv) { Remove-Item $ttsVenv -Recurse -Force }

            Invoke-Python -ArgList @('-m', 'venv', '.venv-tts')
            if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }

            & $ttsPy -m pip install --upgrade pip | Out-Null

            Write-Host "  Installing piper-tts, rvc-python, pyworld..."
            & $ttsPy -m pip install piper-tts pyworld numpy scipy soundfile rvc-python
            if ($LASTEXITCODE -ne 0) { throw "TTS package install failed" }

            if (-not $Cpu) {
                Write-Host "  Installing CUDA torch..."
                & $ttsPy -m pip install "torch==2.6.0+cu124" "torchvision==0.21.0+cu124" --index-url https://download.pytorch.org/whl/cu124
                if ($LASTEXITCODE -ne 0) { throw "torch (cu124) install failed" }
            }

            $voicesDir = Join-Path $ttsVenv 'piper_voices'
            New-Item -ItemType Directory -Force -Path $voicesDir | Out-Null
            Push-Location $voicesDir
            try {
                Write-Host "  Downloading Piper voice models..."
                & $ttsPy -m piper.download_voices en_US-hfc_male-medium
                & $ttsPy -m piper.download_voices en_US-amy-medium
            } finally {
                Pop-Location
            }

            Write-Host "  Done."
        } catch {
            Write-Warn ".venv-tts setup failed: $($_.Exception.Message)"
        } finally {
            Pop-Location
        }
    }
}

# ---------------------------------------------------------------------------
Write-Step "Summary"

if ($warnings.Count -eq 0) {
    Write-Host "No warnings." -ForegroundColor Green
} else {
    Write-Host "$($warnings.Count) warning(s):" -ForegroundColor Yellow
    foreach ($w in $warnings) { Write-Host "  - $w" }
}

Write-Host ""
Write-Host "Still manual - each needs a free personal account/API key:"
Write-Host "  OpenSubtitles API key -> profanity_filter\opensubtitles.key (see profanity_filter\README.md, step 5)"
Write-Host "  TMDB API key          -> profanity_filter\tmdb.key          (see profanity_filter\README.md, step 6)"
Write-Host ""
Write-Host "Try it:"
Write-Host "  cd voice_to_text; .\stt.ps1 `"C:\path\to\recording.m4a`""
Write-Host "  cd profanity_filter; .\clean.ps1 `"C:\Media\Movie.mkv`" --dry-run"
