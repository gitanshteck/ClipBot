# Lightweight setup for the transcription-only pack. Run from PowerShell in
# this folder. Installs only faster-whisper (+ its CUDA-enabled ctranslate2
# dependency) - nothing else from the main ClipBot project is needed here.

$ErrorActionPreference = "Stop"

function Section($title) {
    Write-Host ""
    Write-Host "=== $title ===" -ForegroundColor Cyan
}

Section "Python"
try {
    $pyver = (python --version) 2>&1
    Write-Host "Found: $pyver"
} catch {
    Write-Host "Python not found on PATH. Install 3.10+ from python.org or 'winget install Python.Python.3.12', then reopen this shell." -ForegroundColor Red
    exit 1
}

Section "NVIDIA driver / CUDA"
try {
    $smi = nvidia-smi 2>&1
    if ($LASTEXITCODE -ne 0) { throw "nvidia-smi failed" }
    $smi | Select-String "CUDA Version" | ForEach-Object { Write-Host $_.Line.Trim() }
} catch {
    Write-Host "nvidia-smi not found or failed. Install the current NVIDIA driver first:" -ForegroundColor Red
    Write-Host "  https://www.nvidia.com/Download/index.aspx"
    Write-Host "CTranslate2's CUDA support is bundled in the pip wheel - you need the driver only, not a separate CUDA toolkit install."
    exit 1
}

Section "Install faster-whisper"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

Section "Verify CUDA is visible to CTranslate2"
$check = python -c "import ctranslate2; n = ctranslate2.get_cuda_device_count(); print(n)"
if ($check -match "^\d+$" -and [int]$check -gt 0) {
    Write-Host "CTranslate2 sees $check CUDA device(s). --device auto will use the GPU automatically." -ForegroundColor Green
} else {
    Write-Host "CTranslate2 reports 0 CUDA devices. Transcription will silently fall back to CPU." -ForegroundColor Red
    Write-Host "Check: NVIDIA driver current, no other process holding the GPU."
}

Section "Smoke test"
Write-Host "Copy a short audio.wav here (or point --audio at one on a network share) and run:"
Write-Host ""
Write-Host "  python remote_transcribe.py --audio audio.wav --out transcript.json --max-seconds 120" -ForegroundColor White
Write-Host ""
Write-Host "This transcribes just the first 2 minutes so you can confirm the pipeline works and read the realtime factor before committing to a multi-hour file."
