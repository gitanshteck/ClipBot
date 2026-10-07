# ClipBot

Local pipeline that turns Kick and YouTube livestream VODs into candidate clips
for editing.

**Status: the full pipeline (download → cut → reel) and the review/editor
dashboard are built and in daily use** against real gitanshteck VODs.

Kick serves up to **720p60** (~1.5 GB/hour); there's also an `audio_only` format
(~75 MB/hour) worth considering later if transcription should run before the
video is fetched.

## Pipeline

| # | Stage | Command | Status |
|---|-------|---------|--------|
| 1 | Download VOD (yt-dlp, kick-dl fallback) | `download` | done |
| 2 | Extract audio (`ffmpeg -vn`) | `audio` | done |
| 3 | Transcribe (faster-whisper large-v3) | `transcribe` | done |
| 4 | Find clips (Claude + editable rubric) | `analyze` | done |
| 5 | Cut clips (ffmpeg) | `cut` | done |
| 5b | Author + render vertical 9:16 reels with chat/effects | `reel` | done |
| 6 | Delete the full VOD | `cleanup` | done |

The table is the main path. Chat harvest (1b), speaker diarization (2b), Hinglish
captions (3c) and compilations (5c) exist too. For a **YouTube** stream, stage 1
fetches the audio only and nothing is downloaded as video — see
[YouTube](#youtube).

## Setup

All three external tools are installed on this machine already. To reproduce
elsewhere:

```bash
pip install -r requirements.txt
```

```bash
npm install -g kick-dl
```

```bash
winget install --id Gyan.FFmpeg --exact
```

Notes, each learned the hard way:

- **The `curl-cffi` extra on yt-dlp is required, not optional.** Kick is behind
  Cloudflare and returns `403 Forbidden` to yt-dlp's normal HTTP client. The
  `--impersonate chrome` flag (added automatically, see `download.impersonate`)
  is what makes Kick work at all.
- **Do not `pip install kick-dl`.** The PyPI package of that name is an unrelated,
  stale 0.1.0 wrapper that *depends on* yt-dlp and pins it back to 2024.12.23 —
  useless as a fallback and it silently downgrades your working downloader. The
  real tool is the Node CLI, installed via npm above.
- **ffmpeg is not a pip package.** winget is easiest on Windows; otherwise grab a
  build from <https://www.gyan.dev/ffmpeg/builds/> and either put its `bin` on
  PATH or set `tools.ffmpeg` / `tools.ffprobe` in `config/settings.json`. After a
  winget install, **open a new shell** before the PATH change is visible.
- **Python 3.10+ is recommended.** This runs on 3.9, but yt-dlp now prints a
  deprecation warning on every invocation and will drop 3.9 support.

Check what's wired up:

```bash
python -m clipbot list
```

## Usage

Whole pipeline:

```bash
python -m clipbot run https://kick.com/gitanshteck/videos/<uuid>
```

The URL **must** be the `kick.com/<channel>/videos/<uuid>` form — that's what
yt-dlp's `KickVODIE` extractor matches. The shorter `kick.com/video/<uuid>` form
falls through to the generic extractor and 404s.

Any stage on its own — this is the point of the workspace layout, e.g. re-running
the Claude analysis against an existing transcript without re-downloading:

```bash
python -m clipbot analyze --workspace yourchannel-<video-id>
```

Other commands:

```bash
python -m clipbot info --workspace yourchannel-<video-id>
```

Add `-v` for debug logging (including the exact ffmpeg/yt-dlp command lines) and
`--force` to redo a stage whose output already exists.

## YouTube

A YouTube stream goes through the same pipeline **without downloading the
video**. ClipBot fetches the audio only (~0.3 GB for a 4-hour stream, against
~15 GB of 1080p), transcribes and analyses it as usual, and the dashboard plays
the video from YouTube itself through an embedded player. Use the **YouTube** tab
in the dashboard (paste a link, or browse a channel's streams), or:

```bash
python -m clipbot run "https://www.youtube.com/watch?v=<video-id>"
```

Accepted links are `youtube.com/watch?v=…`, `youtu.be/…` and `youtube.com/live/…`;
a channel, playlist or Short link is refused with a message saying so. The
workspace is `work/yt-<video-id>/`.

**What works today:** the whole find-clips pipeline (audio → transcript →
candidates), reviewing candidates and adjusting their in/out points against the
embedded player with the usual keyboard shortcuts, and **cutting clips and
rendering compilations straight from YouTube**. "Cut approved" (called "Fetch
approved clips" on the workspace page) and the compile page's Render fetch only
the seconds they need, so the full video is never downloaded: on the test video a
10-second clip 1000 seconds in took about a second to fetch. Compilations
download their segments and join them into one file you can save from the compile
page.

Two things to know about those cuts. They are **always re-encoded** (with `cut.*`
for clips and `compile.*` for compilations), never stream-copied: YouTube serves
audio and video as separate streams, and a copy of them measured badly (clips
start seconds early with silence at the front, and joined segments had timestamp
collisions at the seams). Re-encoding is frame-exact and joins cleanly.

How long it takes depends mostly on the video. On a 720p30 test video it ran at
roughly 9 to 10 times realtime. On a real 3.5-hour 1080p60 stream, a 10-second
clip took 2 to 6 seconds to fetch and encode, but a compilation segment at the
default `compile.preset` (`slow`) ran at only about half of realtime (a 45-second
segment took about 100 seconds), so a long 1080p60 compilation is limited by
encoding, not by the download. If that is too slow, try `compile.preset`
`medium`: a 30-second segment at `medium` (with `crf` 20 rather than 18) ran at
about 1.7 times realtime in the same test.

A dropped or stalled connection is retried automatically, and a segment that
still comes back incomplete is fetched once more and then reported as an error.
It is never saved as a broken clip. And all the segments of one compilation must
come from videos with the same resolution and frame rate, and can't be mixed with
segments cut from a downloaded Kick VOD.

**What doesn't yet:** chat overlays (YouTube chat isn't harvested), and a
full-video download for YouTube: nothing is kept locally, so if YouTube ever
stops serving a stream to yt-dlp there is no local copy to fall back on. The
Doctor's **Test YouTube** check is how you find out. **Reels are Kick-only by
design:** the crop editor paints video frames into a canvas, which a YouTube
embed can't provide.

YouTube is harder on downloaders than Kick, so setup needs more than
`pip install -r requirements.txt`:

- **A current yt-dlp, which needs Python 3.10+.** ClipBot only runs the `yt-dlp`
  program, so install it under a newer Python than the one this project runs on
  and point ClipBot at it:

  ```bash
  "C:\Path\To\Python313\python.exe" -m pip install -U "yt-dlp[default,curl-cffi]"
  ```

  then set the `CLIPBOT_YT_DLP` environment variable to the full path of the new
  `yt-dlp.exe` (it's in that Python's `Scripts` folder). An old yt-dlp fails on
  YouTube with `The page needs to be reloaded`.
- **A JavaScript runtime**, which yt-dlp uses to solve YouTube's challenges. Deno
  2.3+ is what it looks for by default:

  ```bash
  winget install DenoLand.Deno
  ```

  (Node 22+ also works if you set `download.youtube.js_runtime` to `"node"`.)
  **Open a new shell** afterwards so the PATH change is visible.
- **ffmpeg 8.1 or newer**, for cutting clips straight from YouTube. It needs
  ffmpeg's `-request_size` option, which first shipped in 8.1: without bounded
  requests YouTube throttles every read to a crawl, so ClipBot refuses to start
  the cut on an older ffmpeg and says so. The Gyan.FFmpeg build from the setup
  section above (8.1.2 when this was written) has it; check `ffmpeg -version` if
  you installed ffmpeg some other way.
- If YouTube asks you to prove you're not a bot, set
  `download.youtube.cookies_from_browser` to a browser you're signed in to
  (Firefox avoids Chrome's locked cookie database).

The dashboard's **Doctor** dialog checks all of this (yt-dlp's version, the
JavaScript runtime, ffmpeg's `-request_size`), and its **Test YouTube** button
asks YouTube for a public video's format list, the one check that proves
extraction works. YouTube changes often; when it breaks, updating yt-dlp is the
first thing to try.

The video must **allow embedding** (YouTube Studio → the video → Show more →
Allow embedding) for the dashboard to play it. If it doesn't, the page says so.

## Workspace layout

Each VOD gets a directory under `work/`: `<channel>-<video-id>` for Kick,
`yt-<video-id>` for YouTube. Stages communicate only through these files, never
by calling each other:

```
work/<channel>-<video-id>/
  state.json        what's done so far, platform, source URL, title, duration
  video.mp4         the VOD (deleted by stage 6). YouTube workspaces have none
  source_audio.m4a  YouTube only: the audio download, deleted once audio.wav exists
  audio.wav         16 kHz mono PCM (kept)
  transcript.json   segment-level timestamps (kept)
  candidates.json   Claude's clip picks (kept)
  clips/            cut clips (kept), with reels/ alongside
  clips.json        your review decisions, ratings, crops and effects
  manifest.json     per-clip description + rationale
  manifest.csv      same, spreadsheet-friendly
```

Because stage boundaries are files on disk, swapping a piece out — a different
transcription model, say — means writing a new module that produces the same
`transcript.json`, with nothing downstream changing.

Two directories under `work/` are **not** workspaces. Both start with an
underscore, which is what keeps them out of the workspace listing:

```
work/_cache/          emote and badge artwork, plus cached proxy previews
work/_library/        the editing library - shared by every stream
```

## Editing library and clip effects

The review page is also the editor. On top of the crop and the chat overlay, a
clip can carry effects: a punch-in zoom, a camera shake, an impact flash, a
freeze on the last frame, a speed change, a sound effect dropped on a timestamp,
a looping music bed ducked under speech, a sticker, and hook text.

The material those effects use lives in **one folder shared across every
stream**, so a sound is added once and reachable from every VOD you ever cut:

```
work/_library/
  sfx/          one-shot sounds - airhorn, boom, bruh, impacts
  music/        beds; they loop automatically under a clip
  stickers/     PNG / GIF / WebP overlays with transparency
  fonts/        .ttf / .otf for hook text
  presets/      saved effect chains, e.g. "hype intro"
  index.json    a disposable cache - the files are the truth
  meta.json     your renames and tags; hand-editable
```

Drop files in, then hit **Rescan** in the review page (or run
`python -m clipbot library scan`). There is no upload step and no new
dependency.

Assets are identified by their **contents**, not their path, so renaming or
moving a file keeps every clip that uses it working. Deleting one is remembered
for 30 days, so the page tells you a sound is missing rather than an effect
quietly vanishing.

```bash
python -m clipbot library scan          # re-read the folders
python -m clipbot library list          # what's in there
python -m clipbot library hash <file>   # a ready-to-paste manifest entry
python -m clipbot library fetch         # download the CC0 starter pack
python -m clipbot library licenses      # where every asset came from
```

`config/library_starter.json` ships **empty on purpose** — see the notes inside
it. Pixabay and Freesound hand out download URLs from client-side API calls, so
they cannot be harvested into a durable manifest, and an entry with a guessed
`sha256` would fail on every fetch. Drop sounds in by hand (that alone is enough
to use them), and use `library hash` if you want them recorded for rebuilding
elsewhere.

Two things worth knowing about effects:

- **Times are absolute stream times.** Nudging a clip's in/out points never
  moves an effect off the moment you placed it on.
- **A saved chain is portable.** It stores offsets from an anchor, not
  timestamps, so "hype intro" applies at the playhead on any clip in any stream.

The vertical preview has two modes. *Geometry* is the instant canvas — still the
right tool for dragging crops. *Effects* renders a short proxy through the real
ffmpeg filter graph at a third of the canvas, so what you see cannot drift from
what renders. It never runs on its own; you ask for it.

## Configuration

`config/settings.json` — paths to external binaries, audio format, model names,
clip padding. Every value can be overridden per-run by an environment variable
(see `ENV_OVERRIDES` in [config.py](clipbot/config.py)); handy for pointing at a
one-off ffmpeg build:

```bash
CLIPBOT_FFMPEG=/path/to/ffmpeg python -m clipbot audio --workspace <slug>
```

`config/rubric.md` — **your** clip-worthiness criteria. Nothing in the pipeline
code knows what makes a clip good; the rubric is read at runtime and dropped into
the prompt. Edit it freely, no code changes needed.

`config/analysis_prompt.md` — the prompt template wrapping the rubric. Edit the
wording, but keep the `{{PLACEHOLDERS}}` and the JSON schema, since the pipeline
parses the response.

## Transcription

**This machine has no usable GPU for transcription.** CTranslate2 (what
faster-whisper runs on) is CUDA-only — there is no ROCm/AMD backend — so the
RX 7800 XT sits idle and `large-v3` runs on the Ryzen 5 7600 at `int8`. Verified:
`ctranslate2.get_cuda_device_count()` returns 0.

Measure before committing to a long run:

```bash
python -m clipbot transcribe --workspace <slug> --max-seconds 600
```

That transcribes the first 10 minutes to `transcript.bench.json`, reports a
realtime factor, and extrapolates to an hour of stream. It does *not* mark the
stage complete, so it won't interfere with a real run.

If the number is too slow, the swap is whisper.cpp with its Vulkan backend, which
*can* use the Radeon. Write a module that emits the same `transcript.json` and
nothing downstream changes.

The model (~3 GB for `large-v3`) is cached under
`C:\Users\<you>\.cache\huggingface`. C: is the smaller drive on this machine, so
set `HF_HOME` to a D: path if space gets tight.

### Measured on this machine

First benchmark, `large-v3` / `int8` / 6 CPU threads, 10 minutes of real stream
audio: **0.79x realtime** — a 73-minute VOD takes roughly 90 minutes. Tolerable
as an overnight or background job; painful if you want same-session turnaround.

### Known quality issues (real, observed)

Benchmarking the **first** 10 minutes of a VOD turned out to be the worst
possible sample, and it surfaced two genuine problems:

1. **Whisper transcribes background music as speech.** The opening slice produced
   14 segments of Hindi song lyrics, immediately followed by the streamer saying
   "I haven't heard this song in a while". Music playing under the stream becomes
   confident-looking transcript text. `no_speech_threshold`,
   `compression_ratio_threshold` and `log_prob_threshold` are now exposed in
   settings as guards, but none of them fully solve it.
2. **Hallucination loops on non-speech.** The same slice repeated one phrase
   three times across minutes, then emitted `आईड` three times in a row. This is
   standard Whisper behaviour over music and silence; `vad_filter` and
   `condition_on_previous_text: false` reduce it but don't eliminate it.

Two consequences worth knowing:

- **Benchmark a mid-stream slice, not the opening** — use `--start-seconds`. A
  stream's first minutes are usually a waiting screen with music, which is both
  unrepresentative and the case Whisper handles worst.
- **`avg_logprob` is per decoding window, not per segment.** faster-whisper gives
  every segment in a window the same score, so `low_confidence` is really a
  window-level flag. Observed runs of 7–8 consecutive segments sharing one value.
  Don't read it as a precise per-line confidence.

Forcing `language: "hi"` also transliterates English speech into Devanagari
("next lockdown" → `नेक्स्ट लॉक टाउंड`). Readable to Claude, but if it hurts
analysis quality, try `language: null` (or `CLIPBOT_WHISPER_LANGUAGE=auto`).

### Low-confidence segments

Segments are marked `"low_confidence": true` and **kept** — never dropped — when
either signal fires. Each carries a `flags` list saying which:

- `low_logprob` — `avg_logprob` below `transcribe.low_confidence_threshold`
  (default `-0.7`).
- `implausible_duration` — segment longer than `transcribe.max_segment_seconds`
  (default `30`). A single 60-second "utterance" isn't speech, it's the decoder
  losing the plot over music.

**Both thresholds are calibrated against real audio from this channel**, not
guessed. On a 10-minute mid-stream slice, hallucinated windows scored `-0.72` to
`-0.90` and ran 10–64s, while genuine speech scored `-0.23` to `-0.58` in 2–15s
segments. The rules flag 8 of 35 segments there, and re-scoring confirms the
split is clean: everything flagged is garbage, everything kept is real speech.

The default was originally `-1.0`, which flagged **nothing** on that slice
despite obvious hallucination — worth knowing if you retune it.

The rubric tells Claude to treat flagged text as approximate rather than wrong.

Knobs worth trying in `config/settings.json` if quality disappoints:

- `language` — currently pinned to `"hi"`. Set to `null` to auto-detect per file.
- `initial_prompt` — a sentence of representative Hinglish primes the model's
  vocabulary and can help noticeably.
- `vad_filter` — on by default; skips silence, which is a real speedup on streams
  with dead air.

## Analysis (stage 4)

Needs `ANTHROPIC_API_KEY` in the environment:

```bash
setx ANTHROPIC_API_KEY "sk-ant-..."
```

The tuning loop this is built around — edit the rubric, re-run against the
transcript you already have, no re-download and no re-transcribe:

```bash
python -m clipbot analyze --workspace <slug> --force
```

**The transcript is the cached prefix, the rubric is not.** That ordering is
deliberate: prompt caching is a prefix match, so putting the (stable) transcript
first and the (changing) rubric second means a rubric edit re-bills only the
rubric — roughly 10% of input cost on repeat runs within the cache TTL. The
`{{CACHE_BREAKPOINT}}` marker in `config/analysis_prompt.md` is where the split
happens; if you reorder that file, you lose the saving.

`candidates.json` records a `rubric_sha1` alongside the clips, so you can tell
which version of your criteria produced a given set of picks.

**Response parsing is defensive by necessity.** `claude-sonnet-4-6` does not
support schema-enforced structured outputs (those need Sonnet 5 / Opus 4.8+), so
the reply is parsed out of text — handling bare JSON, ```` ```json ```` fences,
and JSON wrapped in prose. Switching `analyze.model` to `claude-sonnet-5` or
`claude-opus-5` would be a drop-in upgrade.

Returned clips are checked **structurally only** — non-numeric or backwards
ranges are dropped, ranges past the end of the stream are trimmed, overlaps are
trimmed so the cutting stage never produces duplicate footage, and clips are
sorted. Clip *length* only produces a warning: how long a clip should be is a
rubric question, not a code question.

## Notes

- **Hindi/English code-switching.** The transcription stage will flag
  low-confidence segments rather than dropping them, and the rubric tells Claude
  to treat flagged text as approximate. Expect some segments to be rough.
- **Disk.** The VOD is only deleted after clips are cut (stage 6). Audio is
  extracted immediately after download so nothing later in the pipeline needs the
  video except the cutting stage.
- **Kick breakage.** yt-dlp periodically breaks on Kick site changes. Try
  `pip install -U yt-dlp` first; the kick-dl fallback fires automatically and can
  be turned off via `download.fallback_to_kick_dl`.
