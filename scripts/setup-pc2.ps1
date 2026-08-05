# ClipBot setup for the GTX 1660 Super machine.
#
# Run this from a PowerShell window on that PC, from inside the folder where
# you extracted clipbot-code.zip (i.e. this script should sit next to
# clipbot/, config/, requirements.txt).
#
# What this does NOT do: nothing here is destructive. It installs into
# whatever Python/pip is on PATH and downloads a couple of GB of model
# weights on first transcribe run. Re-run it safely if a step fails partway.

$ErrorActionPreference = "Stop"

function Section($title) {
    Write-Host ""
    Write-Host "=== $title ===" -ForegroundColor Cyan
}

Section "Python"
try {
    $pyver = (python --version) 2>&1
    Write-Host "Found: $pyver"
    if ($pyver -match "3\.9") {
        Write-Host "Python 3.9 works, but 3.10+ is recommended (yt-dlp deprecation warning, no other blocker)." -ForegroundColor Yellow
    }
} catch {
    Write-Host "Python not found on PATH. Install 3.10+ from python.org or winget install Python.Python.3.12, then re-open this shell." -ForegroundColor Red
    exit 1
}

Section "NVIDIA driver / CUDA"
try {
    $smi = nvidia-smi 2>&1
    if ($LASTEXITCODE -ne 0) { throw "nvidia-smi failed" }
    $smi | Select-String "CUDA Version" | ForEach-Object { Write-Host $_.Line.Trim() }
} catch {
    Write-Host "nvidia-smi not found or failed. Install the current NVIDIA driver first:" -ForegroundColor Red
    Write-Host "  https://www.nvidia.com/Download/index.aspx (GeForce GTX 1660 Super)"
    Write-Host "CTranslate2's CUDA support is bundled in the pip wheel - you need the driver, not a separate CUDA toolkit install."
    exit 1
}

Section "ffmpeg"
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpeg) {
    Write-Host "Installing ffmpeg via winget..."
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    Write-Host "ffmpeg installed. You will need to CLOSE and REOPEN this PowerShell window before it's on PATH." -ForegroundColor Yellow
    Write-Host "Re-run this script after reopening." -ForegroundColor Yellow
    exit 0
} else {
    Write-Host "Found: $($ffmpeg.Source)"
}

Section "Python packages"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-server.txt
Write-Host "If yt-dlp needs the curl-cffi extra and it didn't pull automatically:"
Write-Host '  pip install "yt-dlp[default,curl-cffi]"'

Section "Verify CUDA is visible to CTranslate2"
$check = python -c "import ctranslate2; n = ctranslate2.get_cuda_device_count(); print(n)"
if ($check -match "^\d+$" -and [int]$check -gt 0) {
    Write-Host "CTranslate2 sees $check CUDA device(s). transcribe.device=auto will use the GPU automatically - no settings.json change needed." -ForegroundColor Green
} else {
    Write-Host "CTranslate2 reports 0 CUDA devices. Transcription will silently fall back to CPU." -ForegroundColor Red
    Write-Host "Check: NVIDIA driver current, no other process holding the GPU, ctranslate2 installed with CUDA support (the default pip wheel includes it)."
}

Section "ANTHROPIC_API_KEY"
if ($env:ANTHROPIC_API_KEY) {
    Write-Host "Set for this session." -ForegroundColor Green
} else {
    Write-Host "Not set. Only needed for the analyze stage (stage 4) - transcription works without it." -ForegroundColor Yellow
    Write-Host '  setx ANTHROPIC_API_KEY "sk-ant-..."   (then reopen the shell)'
}

Section "Next: measure the real speedup before trusting any estimate"
Write-Host "Copy an audio.wav from an existing workspace on the other PC, or run the"
Write-Host "download+audio stages here, then benchmark a 10-minute mid-stream slice:"
Write-Host ""
Write-Host "  python -m clipbot transcribe --workspace <slug> --max-seconds 600 --start-seconds 2100" -ForegroundColor White
Write-Host ""
Write-Host "The logged realtime factor is your actual number for this card - use it" -ForegroundColor White
Write-Host "instead of any estimate." -ForegroundColor White
