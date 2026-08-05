# ClipBot transcription pack (PC2)

Runs just the transcription stage on this machine's GPU. Not the ClipBot
project — three files: `remote_transcribe.py`, `requirements.txt`,
`setup.ps1`. No ffmpeg, no yt-dlp, no dashboard, no API keys.

## Setup

```powershell
.\setup.ps1
```

Checks Python, checks the NVIDIA driver, installs `faster-whisper`, and
confirms CTranslate2 can actually see the GPU (it silently falls back to CPU
otherwise, so don't skip that check in the output).

## Get audio.wav here

`audio.wav` lives in the workspace on PC1 at `work/<slug>/audio.wav`. Either:

- **Network share**: map PC1's `ClipBot` folder as a drive on PC2 (or the
  reverse), then just `Copy-Item` the file.
- **Claude Code**: since you're putting Claude Code on this machine too, you
  can just ask it to pull the file over — same LAN, both machines reachable
  by hostname/IP.

A 4.6-hour stream's `audio.wav` (16kHz mono PCM) is roughly 500MB — a couple
of minutes over gigabit LAN.

## Run it

Smoke test first (first 2 minutes only, confirms the model loads and the GPU
is actually being used):

```powershell
python remote_transcribe.py --audio audio.wav --out transcript.json --max-seconds 120
```

Check the printed `device=` — if it says `cpu` here, stop and fix the CUDA
check in `setup.ps1`'s output before running the full file. Then the real
run:

```powershell
python remote_transcribe.py --audio audio.wav --out transcript.json
```

Defaults match `config/settings.json` on the main PC exactly (`large-v3`,
`language=hi`, `beam_size=5`, VAD on, the same low-confidence thresholds).
Override with flags if needed — run `python remote_transcribe.py --help` for
the full list.

## Bring transcript.json back

Copy the output `transcript.json` into PC1's workspace at
`work/<slug>/transcript.json`, then either:

- Run `python -m clipbot analyze --workspace <slug>` directly (it reads
  `transcript.json` off disk, same as if the local transcribe stage had
  produced it), or
- Open the dashboard — the workspace page will show the transcribe stage as
  already done and let you continue from analyze.

You do **not** need to mark anything in `state.json` by hand — the analyze
stage only checks that `transcript.json` exists and is well-formed.

## Keeping this in sync

`remote_transcribe.py` intentionally duplicates the whisper options and
low-confidence flagging logic from `clipbot/stages/transcribe.py` instead of
importing it, so this pack has zero dependency on the rest of the codebase.
If you ever change `transcribe.*` thresholds in the main `config/settings.json`,
update the matching defaults in this script's `argparse` block too — nothing
enforces that they stay identical.
