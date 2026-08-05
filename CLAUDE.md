# CLAUDE.md — ClipBot technical map

This file is for a coding agent (or a human) that needs to work in this
codebase quickly. It documents **how the system is built**, not how to use
it — for usage, setup, and the tuning workflow, read [README.md](README.md).

**Ground truth is the code.** If anything here disagrees with `clipbot/`,
`config/settings.json`, or `clipbot/cli.py`, the code wins — this file (and
occasionally the README) can drift. See "Keeping this file honest" at the
bottom.

> Kept up to date by the `clipbot-docs-sync` skill at
> `.claude/skills/clipbot-docs-sync/SKILL.md`. Last verified against the code:
> 2026-08-06.

## What this actually is, right now

ClipBot turns a Kick livestream VOD into reviewed, cut, vertical-ready clips.
**The README's top banner ("stages 1–2 built... transcription/analysis/cutting
scaffolded but not implemented") is stale and wrong** — it was never updated
after the rest of the system was built. The real state:

- All 6 pipeline stages (download, audio, transcribe, analyze, cut, cleanup)
  are fully implemented, plus a chat-harvest stage (1b), an opt-in speaker
  diarization stage (2b), a Hinglish-caption transliteration stage (3c), and
  a reel-render stage (5b) the README's table doesn't even list.
- There is a full local web dashboard/editor (`clipbot/server/`, FastAPI) —
  not just a CLI. It's the primary way clips actually get reviewed, cropped,
  effect-decorated, and re-rendered as 9:16 reels.
- There's a cross-stream asset library (`clipbot/library.py`) for sound
  effects, music, stickers, fonts, and saved effect presets.
- Everything below reflects the code as it stands, not the README banner.

Rest of the README (setup instructions, the "Transcription", "Analysis",
"Editing library" sections) **is** accurate and up to date — only the opening
status line/table is stale.

## Core architecture

**Stages never call each other. They only read/write files under a
`Workspace`.** This is the single most important design decision in the
codebase — everything else follows from it.

```
CLI (clipbot/cli.py)  ─┐
                        ├──> Workspace (clipbot/workspace.py) ──> work/<slug>/*.json, *.mp4, *.wav
Dashboard (server/*)  ─┘         ^
                                  │
                         Settings (clipbot/config.py) <── config/settings.json + CLIPBOT_* env vars
```

- **`Workspace`** (`clipbot/workspace.py`): one per VOD, rooted at
  `work/<channel>-<video-id>/`. Owns path properties (`audio_path`,
  `transcript_path`, `candidates_path`, `clips_path`, `chat_path`, ...),
  atomic JSON read/write (`write_json`/`read_json`, write-tmp-then-`.replace()`),
  and `state.json` helpers (`read_state`/`update_state`/`mark_stage`/
  `stage_done`). One `threading.RLock` per resolved workspace root
  (`_ROOT_LOCKS`) guards read-modify-write races, because the dashboard can
  run stages concurrently with the CLI touching the same workspace.
- **`Settings`** (`clipbot/config.py`): thin dotted-path wrapper over
  `config/settings.json`. Every leaf can be overridden by a `CLIPBOT_*` env
  var (see `ENV_OVERRIDES`). `Settings.tool(name)` resolves an external
  binary path — a bare name goes through `PATH`, a relative path resolves
  against the **project root** (not cwd), which is how the vendored ffmpeg
  build keeps working regardless of where you launch from.
- **Two front ends, one engine**: `clipbot/cli.py` (`python -m clipbot ...`)
  and `clipbot/server/app.py` (`python -m clipbot.server`, the dashboard)
  both call the exact same `clipbot/stages/*.py` functions. Neither is a
  wrapper around the other. The dashboard adds job queueing/SSE progress
  (`clipbot/server/jobs.py`, `clipbot/progress.py`) on top; the CLI passes a
  no-op `NULL_PROGRESS` sink.
- **Swappable stages by contract, not by interface**: nothing downstream of
  `transcribe.py` cares how `transcript.json` was produced. Multiple module
  docstrings say explicitly "write a new module that emits the same JSON
  shape, nothing downstream changes" (true for transcription model swaps,
  chat harvesters, etc).

## Repository map

```
clipbot/
  cli.py            argparse CLI — every subcommand, the single source of truth
                     for what commands exist (run, download, chat, chat-sync,
                     audio, transcribe, analyze, transliterate, cut, reel,
                     manifest, cleanup, info, list,
                     library {scan,list,presets,licenses,hash,fetch,path})
  __main__.py        `python -m clipbot` -> cli.main()
  config.py          Settings / load_settings() / ENV_OVERRIDES
  workspace.py        Workspace, slug_for_url(), the Kick URL regex
  utils.py           logging setup, run_command() subprocess wrapper, slugify(),
                     format_timestamp(), StageError/ToolMissingError
  specerror.py       SpecError(ValueError) — shared by reelspec.py and fxspec.py
                     on purpose, to avoid a circular import; ValueError-ness is
                     load-bearing (server PATCH routes map it to HTTP 400)
  manifest.py        write_manifest() -> manifest.json + manifest.csv (utf-8-sig)
  review.py          clips.json state machine: reconcile(), update_clip(),
                     add_manual_clip(), clips_for_cutting(), counts()
  reelspec.py        vertical-reel geometry: normalize()/resolve()/build_filter()/
                     build_argv() — pure math, no IO
  fxspec.py          per-clip effects schema + ffmpeg filter builders — pure
                     math, no IO
  library.py         cross-stream asset library: scan(), asset_id(), presets,
                     speaker profiles, fetch_starter()
  speakers.py        per-workspace segment-id -> speaker-id overlay
                     (speaker_map.json): assign_range(), bulk_from_diarization(),
                     resolved_segments(), speaking_spans()
  speakerfx.py       speaker avatar/ring overlay: resolve_speakers() + the
                     filter-chain builder - NOT pure like reelspec/fxspec,
                     since it synthesizes circle-cropped avatar and ring PNGs
                     via Pillow (cached in _cache/avatars/)
  chatrender.py      chat messages -> PNG overlay frames (Pillow, not libass)
  captionrender.py   Hinglish caption segments -> PNG overlay frames (Pillow;
                     smaller mirror of chatrender.py, no emote/badge images)
  chatsync.py        estimate_offset() — correlate chat rate against audio
                     energy to find chat_offset_seconds
  ffrun.py           shared ffmpeg subprocess runner with progress + cancellation
  preview.py         PreviewRunner — the dashboard's live "Effects" proxy preview
  progress.py        Progress / NULL_PROGRESS — CLI-safe, dashboard-aware progress
                     reporting with cancellation and ETA
  stages/
    download.py       stage 1  (yt-dlp; kick-dl fallback currently disabled)
    chat.py            stage 1b (Kick chat REST API harvest)
    audio.py          stage 2  (ffmpeg -vn to 16kHz mono PCM)
    diarize.py         stage 2b (opt-in: pyannote speaker diarization)
    transcribe.py      stage 3  (faster-whisper, confidence flagging)
    transliterate.py   stage 3c (Claude API, Devanagari -> Hinglish captions)
    analyze.py         stage 4  (Claude API, prompt caching, JSON extraction)
    cut.py             stage 5  (ffmpeg cut, copy or re-encode)
    reel.py            stage 5b (vertical 9:16 re-encode through fx filter graph)
  server/
    app.py            FastAPI app, all HTTP routes, security guard()
    jobs.py           JobRunner (2 worker lanes) + EventBus (SSE)
    media.py          hand-rolled HTTP Range file serving
    __main__.py       `python -m clipbot.server` entry point (uvicorn)
    templates/        Jinja2: base.html, library.html, workspace.html, review.html
    static/           vanilla JS/CSS: app.js, fx.js, reel.js, review.js, *.css
                       (ignore the *.bak files sitting alongside — stale, unused)

config/
  settings.json         all tunables — see "Configuration" below
  rubric.md             user-owned clip-worthiness criteria (zero code coupling)
  analysis_prompt.md    prompt template wrapping the rubric; has {{PLACEHOLDERS}}
  library_starter.json  CC0 asset manifest — ships with an empty assets: [] on
                         purpose (see library.py section)

tests/
  test_fxspec.py               effects schema validation + filter-string invariants
  test_reelspec_invariant.py   pins the effects-free filter graph byte-for-byte
  test_captions.py             captions spec validation, geometry, and the same
                                legacy-path-unaffected invariant for captions
  test_speakers.py             speaker merge/assignment logic, speakers spec
                                validation + slot layout, and the same
                                legacy-path-unaffected invariant for speakers

work/                    gitignored. Per-VOD workspaces + _cache/ + _library/
scripts/setup-pc2.ps1     provisioning script for a second (CUDA-capable) machine
requirements.txt          core pipeline deps
requirements-server.txt   dashboard-only deps (fastapi/uvicorn/jinja2, capped for py3.9)
requirements-diarize.txt  opt-in: pyannote.audio + PyTorch for stages/diarize.py -
                          NOT part of requirements.txt, deliberately (multi-GB
                          install for a stage most workspaces never run). Pins
                          two transitive deps pyannote.audio itself leaves
                          unbounded: `greenlet<=3.2.4` (newer greenlet ships no
                          cp39-win_amd64 wheel, and this machine has no MSVC C++
                          Build Tools to compile it from source) and
                          `huggingface_hub<1.0` (1.0 removed the `use_auth_token`
                          kwarg that pyannote.audio 3.4.0's Pipeline.from_pretrained
                          still passes internally - see diarize.py's section below).
```

## The pipeline, stage by stage

Every stage function takes `(ws: Workspace, settings: Settings, force=False,
...)` and returns the path it produced. All of them check `ws.stage_done(name)`
(via the workspace's `state.json`) and no-op unless `force=True`.

| # | Stage | Module | Produces | Notes |
|---|-------|--------|----------|-------|
| 1 | download | `stages/download.py` | `video.<ext>` | yt-dlp + `--impersonate chrome`; kick-dl fallback wired but disabled (non-functional TUI) |
| 1b | chat | `stages/chat.py` | `chat.json` | Kick REST API, cursor pagination; best-effort, never blocks the rest of `run` |
| 2 | audio | `stages/audio.py` | `audio.wav` | ffmpeg `-vn`, 16kHz mono PCM |
| 2b | diarize | `stages/diarize.py` | `diarization.json` | pyannote, CPU-only here; opt-in, needs `requirements-diarize.txt` + an HF token, never part of `run` |
| 3 | transcribe | `stages/transcribe.py` | `transcript.json` | faster-whisper `large-v3`; flags but never drops low-confidence segments |
| 3c | transliterate | `stages/transliterate.py` | `captions.json` | Claude API, batched; Devanagari transcript -> Hinglish (Latin script) for caption overlays; opt-in, never part of `run` |
| 4 | analyze | `stages/analyze.py` | `candidates.json` | Claude API against the rubric; structural validation only |
| 5 | cut | `stages/cut.py` | `clips/*.mp4` | ffmpeg copy (default) or re-encode; writes into `clips.json[*].output` |
| 5b | reel | `stages/reel.py` | `clips/reels/*.mp4` | vertical 9:16 re-encode through the fx/chat/captions/speakers filter graph |
| 6 | cleanup | `stages/download.py:delete_vod` | deletes `video.<ext>` | refuses if any approved clip/reel isn't rendered yet, unless `--force` |

### download.py

- `download_vod(url, ws, settings, force=False) -> Path`. Skips if
  `ws.video_path()` already resolves to a file.
- yt-dlp invocation: `--no-playlist --newline --write-info-json -f
  <download.format> -o <root>/video.%(ext)s --impersonate chrome <url>`, run
  via `run_command(..., capture=True, tee=True)` (streams to console **and**
  captures for error messages).
- **`--impersonate chrome` is mandatory, not cosmetic**: Kick sits behind
  Cloudflare and 403s yt-dlp's default HTTP client without it.
- The kick-dl fallback (`_download_with_kick_dl`) is real code but is
  **known non-functional**: kick-dl 2.0.0 only ships an interactive TUI
  (`kick-dl start`, no scriptable flags), which would hang a dashboard worker
  on a prompt nobody can answer. `download.fallback_to_kick_dl` is `false` in
  `config/settings.json` for this reason — don't flip it on without a kick-dl
  release that adds non-interactive flags.
- `_read_metadata` pulls from yt-dlp's `video.info.json`: `title`,
  `kick_duration` (**deliberately not written as `duration`** — the audio
  stage's probed value is authoritative; Kick's own figure disagreed by ~31s
  on a real 4.6h VOD), `uploader`, `upload_date`, `stream_started_at` (=
  `info.timestamp`, the anchor `chat.py` needs), `kick_channel_id` (=
  `info.channel_id`, **not** `uploader_id`), plus `video_width/height/fps/format_id`
  (recorded because the format selector is unreliable — one run silently
  returned 160p).
- Warns if `video_height < download.min_height_warn` (480).
- `list_channel_vods(channel, limit=5)` hits
  `kick.com/api/v2/channels/{channel}/videos` to turn a 404 into a helpful
  list of real VOD UUIDs — Kick's live-stream session UUIDs look identical to
  VOD UUIDs but 404 if copied mid-stream.

### chat.py (stage 1b)

- **Not a websocket replay.** Polls Kick's REST endpoint
  `GET /api/v2/channels/{channel_id}/messages`, which returns 25
  newest-first messages plus a `cursor`.
- Two reverse-engineered facts that matter if you ever touch this:
  - `?start_time=<ISO>` is **not pagination** — it returns only messages at
    that exact second (effectively always empty). Every published wrapper
    for this API gets this wrong.
  - `?cursor=<n>` **is** pagination and is a **microsecond unix epoch** and
    is seekable — the harvester jumps straight to the stream's end and walks
    backward rather than paging from the start.
- Channel id must come from `channels/<slug>.id` (same as yt-dlp's
  `channel_id`); `user_id`/`chatroom.id` both return HTTP 200 with zero
  messages, an easy way to wrongly conclude "no chat exists."
- Harvest window: `[stream_started_at - margin, stream_started_at + duration
  + margin]`, `margin = chat.margin_seconds` (300s) — wide on purpose because
  the anchor (when Kick opened the livestream record) is not necessarily
  when recording started, and `chat_offset_seconds` can shift the whole
  track later without a refetch.
- On an empty page, steps the cursor back by `chat.empty_stride_seconds`
  (120s) rather than assuming end-of-stream (a quiet stretch isn't the end).
- **Never writes an empty message list as success.** Zero surviving
  messages writes `{"status": "unavailable", ...}` and raises `StageError` —
  this disambiguates "chat empty" from "fetch never ran."
- `messages_between(doc, start, end, offset=0.0)` is the shared query
  function every downstream consumer uses. Sign convention: raw offsets run
  **ahead** of the VOD's own clock, so correcting means **subtracting** a
  positive `offset` to pull messages earlier onto the VOD timeline.
- `chat.json` (status `"ok"`): `{schema, status, source, channel_id,
  stream_started_at, window: [start_rel, end_rel], fetched_at, pages,
  dropped, count, messages: [{id, offset, at, user: {name, slug, color,
  badges}, text, parts}]}`.

### audio.py

- `extract_audio(ws, settings, force=False, video=None) -> Path`. ffmpeg
  `-vn -sn -dn -ac 1 -ar 16000 -c:a pcm_s16le` → `audio.wav`. 16kHz mono PCM
  is chosen because that's what Whisper resamples to internally anyway.
- `probe_duration()` (ffprobe) sets `state.duration` — **the only duration
  value ever trusted downstream** for clip range clamping.
- `trim_audio(source, out_path, seconds, settings, start=0.0)` — stream-copy
  slice used by `clipbot transcribe --max-seconds` benchmark mode.

### diarize.py (stage 2b, opt-in)

- `diarize_audio(ws, settings, force=False, progress=NULL_PROGRESS) -> Path`.
  Not part of `clipbot run` or the dashboard's `pipeline` job, ever - needs
  `pip install -r requirements-diarize.txt` (PyTorch, multi-GB) and a
  Hugging Face access token (`diarize.hf_token_env` names the env var,
  default `HF_TOKEN`; the model's user agreement must also be accepted on
  HF's site first - see the stage's `HF_TOKEN_HINT` for the exact steps).
- Device resolution mirrors `transcribe.py`'s `_resolve_device` exactly - no
  ROCm backend, so this machine always lands on CPU too, same caveat.
- **Two real dependency gotchas, both measured, not guessed:**
  - `huggingface_hub<1.0` is pinned in `requirements-diarize.txt` - 1.0
    removed the `use_auth_token` kwarg entirely, and pyannote.audio 3.4.0's
    `Pipeline.from_pretrained` still passes it internally. Unpinned, every
    call fails with `TypeError: ... unexpected keyword argument
    'use_auth_token'` before ever reaching the network.
  - **`Pipeline.from_pretrained` does not raise on a bad token or an
    unaccepted gated-model agreement - it logs a lengthy hint and returns
    `None`.** `_load_pipeline` explicitly checks for that and raises a
    `StageError` with the real cause; without the check, the failure surfaces
    three lines later as `TypeError: 'NoneType' object is not callable`,
    which points nowhere near the actual problem.
  - **The same silent-`None` pattern exists one level deeper, for the
    pipeline's *sub*-models.** `speaker-diarization-3.1` depends on
    `pyannote/segmentation-3.0` as a separate gated model - accepting only
    the top-level pipeline's terms is the single most common way this
    fails. Internally, pyannote's own `get_model()` calls
    `Model.from_pretrained()` (same `None`-on-gated-failure behavior) and
    then unconditionally calls `.eval()` on the result with no `None`
    check, so this one surfaces as `AttributeError: 'NoneType' object has
    no attribute 'eval'` instead. `_load_pipeline` catches `AttributeError`
    around the whole `from_pretrained` call and re-raises a `StageError`
    naming the likely cause, since pyannote's own error here names neither
    the missing model nor what to do about it. `HF_TOKEN_HINT` lists both
    gated models' URLs up front so a new setup hits this zero times, not
    once.
- **A single blocking call** (`pipeline(audio_path, **kwargs)`), unlike
  `transcribe.py`'s lazy generator - pyannote exposes no per-segment yield
  point, so there's no mid-run progress or cancellation, only a
  `check_cancelled()` before it starts.
- `diarization.json`: `{model, device, duration, diarize_seconds, turn_count,
  speaker_count, turns: [{start, end, speaker}]}`. `speaker` is a raw
  pyannote cluster label (`SPEAKER_00`, ...) - pyannote clusters voices, it
  doesn't know names. Naming happens afterward, never here.
- On success, best-effort seeds `speaker_map.json` via
  `clipbot.speakers.bulk_from_diarization` (never overwriting an existing
  assignment) - so running this stage alone is usually enough to get usable
  per-segment speaker assignments without a separate manual step.

### transcribe.py

- `transcribe_audio(ws, settings, force=False, audio_path=None,
  out_path=None, mark_stage=True, progress=NULL_PROGRESS) -> Path`.
- Device: `auto` → `cuda` iff `ctranslate2.get_cuda_device_count() > 0`,
  else `cpu` (**no ROCm/AMD backend exists**, so a Radeon GPU always falls
  through to CPU). Compute type `default` → `float16` on cuda, `int8` on cpu.
- Whisper options: `language` ("hi" pinned by default; string
  `"auto"/"none"/"null"/""` normalizes to `None` since env var overrides
  arrive as strings), `beam_size=5`, `vad_filter=True`,
  `condition_on_previous_text=False`, `no_speech_threshold=0.6`,
  `compression_ratio_threshold=2.4`, `log_prob_threshold=-1.0`.
- **Low-confidence flagging** (segments are flagged and **kept, never
  dropped** — a hard invariant repeated across the codebase and rubric):
  - `low_logprob`: `avg_logprob < transcribe.low_confidence_threshold`
    (default **-0.7**).
  - `implausible_duration`: segment longer than
    `transcribe.max_segment_seconds` (default **30.0**) — a "segment"
    running tens of seconds is the decoder losing the plot over music or
    silence, not real speech.
  - Both thresholds were calibrated against real audio from the reference
    channel (hallucinated windows scored -0.72..-0.90 over 10-64s;
    genuine speech scored -0.23..-0.58 over 2-15s). The original -1.0
    threshold flagged nothing on the same slice despite obvious hallucination.
- Streams the model's lazy generator; calls `progress.check_cancelled()` per
  segment so a multi-hour job can be cancelled mid-run.
- `transcript.json`: `{audio_file, model, device, compute_type, language,
  language_probability, duration, segment_count, low_confidence_count,
  low_confidence_threshold, max_segment_seconds, transcribe_seconds,
  realtime_factor, options: {...}, segments: [{id, start, end, text,
  avg_logprob, no_speech_prob, compression_ratio, low_confidence, flags}]}`.
- Docstring explicitly frames this as the module most likely to be swapped
  (whisper.cpp/Vulkan for the idle Radeon, a hosted API) — the contract that
  matters is the `transcript.json` shape, nothing else.

### analyze.py

- `analyze_transcript(ws, settings, force=False, progress=NULL_PROGRESS) -> Path`.
- `build_prompt()`: loads `analyze.rubric_file` (`config/rubric.md`) and
  `analyze.prompt_template` (`config/analysis_prompt.md`), strips `<!-- -->`
  comments from both, substitutes `{{TRANSCRIPT}}` / `{{RUBRIC}}` /
  `{{DURATION}}` / `{{STREAM_TITLE}}`.
- **Prompt caching**: splits the template on the literal marker
  `{{CACHE_BREAKPOINT}}` — everything above (the transcript) is sent with
  `cache_control: {"type": "ephemeral"}`; everything below (the rubric +
  task instructions) is not. This ordering is deliberate: editing only the
  rubric and re-running rebills roughly 10% of input cost within the cache
  TTL. **If you reorder `analysis_prompt.md`, you lose this savings.**
- Calls `anthropic.Anthropic()` — requires `ANTHROPIC_API_KEY` in the
  environment. Model default `claude-sonnet-4-6` (settings.json), `max_tokens`
  32000.
- **Uses `client.messages.stream()`, never `.create()`.** Measured: at
  `analyze.effort: "high"` with adaptive thinking on, a 262-segment
  transcript burned the entire 32000-token budget on reasoning and returned
  **zero output text**, twice, both with `stop_reason=max_tokens`. This is
  why `config/settings.json` ships `analyze.thinking: false`. If you turn
  thinking back on, use `effort: "low"`/`"medium"` and watch for empty
  responses.
- `extract_json(text)`: tolerant parser — strips ` ```json ` fences, falls
  back to `json.loads`, falls back to locating the outermost `{...}` span,
  raises `StageError` with the first 500 chars on total failure (handles
  Claude wrapping the JSON in prose).
- `validate_clips()` is **structural only** — the rubric owns editorial
  judgment, this code does not:
  - drops non-dict items, non-numeric or backwards ranges;
  - clamps `start = max(0, start)`; drops clips starting past `duration`;
    trims `end` to `duration` if it overruns;
  - warns (does not drop) if length is outside
    `[analyze.warn_shorter_than, analyze.warn_longer_than]` (5s–120s);
  - sorts by start, then **trims overlaps** — if clip N starts before clip
    N-1 ends, N's start is pushed to N-1's end (and N is dropped entirely if
    that leaves it under 1.0s) — this is what guarantees the cut stage never
    produces duplicate footage.
- `candidates.json`: `{model, rubric_file, rubric_sha1, transcript_segments,
  stop_reason, usage: {input_tokens, output_tokens,
  cache_creation_input_tokens, cache_read_input_tokens}, clips:
  [{start_time, end_time, description, why}]}`. `rubric_sha1` is what lets
  you tell which version of your criteria produced a given set of picks.
- **`claude-sonnet-4-6` does not support schema-enforced structured
  outputs** (needs Sonnet 5 / Opus 4.8+) — that's the whole reason
  `extract_json` has to be defensive. Switching `analyze.model` to
  `claude-sonnet-5` or `claude-opus-5` would be a drop-in upgrade worth
  revisiting.

### transliterate.py (stage 3c)

- `transliterate_transcript(ws, settings, force=False, progress=NULL_PROGRESS) -> Path`.
  Opt-in and separate from `analyze` on purpose — never run as part of
  `clipbot run` or the dashboard's `pipeline` job kind, since it costs API
  calls a user may not want on every VOD.
- **Why it exists**: `transcribe.py` pins `language: "hi"`, so Whisper writes
  spoken Hindi as Devanagari. `chatrender.py`'s `unshaped_scripts()` already
  documents that Pillow (no libraqm in any published Windows wheel) cannot
  shape Devanagari correctly — so burning the raw transcript into a caption
  overlay was never viable. This stage produces a Latin-script (Hinglish)
  version specifically so `captionrender.py` can render it with the same
  Pillow stack chat overlays already use.
- Segments are batched (`transliterate.batch_size`, default 50) into
  separate Claude calls rather than one call for the whole transcript — a
  multi-hour VOD can have 800+ segments, and this keeps one bad response
  from failing the whole run. A batch that errors or returns unparseable
  JSON just leaves its segments with their original Devanagari text
  (`failed_batches` in the output counts these) rather than aborting.
- Same `client.messages.stream()` discipline as `analyze.py`, same
  `ANTHROPIC_API_KEY` requirement. `transliterate.thinking: false` by
  default — this is a mechanical transliteration task, not a judgment call,
  so thinking buys nothing and only risks the same max-tokens-with-no-output
  failure mode `analyze.py` documents.
- `transliterate.model` defaults to `claude-haiku-4-5`, **deliberately not**
  matching `analyze.model` — cost-sensitive since it's a bulk mechanical
  pass, not a rubric judgment.
- `captions.json`: `{source_transcript_sha1, model, batch_size,
  segment_count, failed_batches, segments: [{id, start, end, text}]}`. Same
  `id`/`start`/`end` as the source transcript segment; `text` is Hinglish
  (or the original Devanagari, for any segment a failed batch fell back on).
- `Workspace.captions_path` → `captions.json`, a sibling of `transcript.json`
  rather than a mutation of it — `transcribe.py` stays the sole writer of
  `transcript.json`, same single-writer-per-file discipline as
  `candidates.json`/`clips.json`.

### cut.py

- `cut_clips(ws, settings, force=False, clip_ids=None, progress=NULL_PROGRESS) -> Path`.
- `_load_clips` prefers `clips.json` (review state) over `candidates.json`;
  falls back to synthesizing approved clips directly from
  `candidates.json` (treating every candidate as approved) so the CLI works
  end-to-end without ever touching the dashboard.
- **Two gotchas that matter if you touch the ffmpeg invocation**:
  1. `-ss` must go **before** `-i` (input seek) even in stream-copy mode —
     with `-c copy` and an *output* seek, ffmpeg decodes and discards
     everything before the cut point (the difference between a 2-second and
     a 2-minute wait on a 73-minute VOD).
  2. Stream copy snaps to the nearest keyframe, so a copy-mode cut can start
     a few seconds early. This is the accepted default trade (instant,
     lossless); `cut.re_encode: true` trades speed for frame accuracy.
- Copy mode: `-c copy -avoid_negative_ts make_zero`. Re-encode mode:
  `-c:v libx264 (cut.encoder) -preset veryfast (cut.preset) -crf 20
  (cut.crf) -c:a aac -b:a 160k`. Always `-movflags +faststart`.
- `_padded_range(clip, settings, duration)`: `start = max(0, clip.start -
  cut.pad_start)`, `end = min(duration, clip.end + cut.pad_end)` — this exact
  function is reused by `reel.py` and by the dashboard's `/reel/plan` and
  `/reel/preview` routes, so padding is always consistent across cut and reel.
- `_fingerprint()` is a hand-listed key (`start|end|pad_start|pad_end|
  encode-or-copy`) — **known limitation**: it does not include `crf`, so
  changing `cut.crf` alone will not trigger a re-cut of existing clips.
  (Contrast with `reel.py`, which fingerprints the actual built argv — see
  below.)
- On a per-clip ffmpeg failure, sets that clip's `status = "failed"` and
  `notes` (truncated to 500 chars) rather than aborting the whole run.
- Output written to `clips.json[*].output`: `{file, bytes, duration, cut_at,
  fingerprint, re_encode}`.
- `uncut_approved(ws)` — approved clips whose `output.file` doesn't exist
  yet; used by `cleanup` (CLI and dashboard) to refuse deleting the VOD
  while cuttable work is outstanding.

### reel.py (stage 5b)

- Deliberately a **separate stage from cut**: cut is a lossless archival
  stream-copy; reel is always a full re-encode through a filter graph.
  Separate state key too — `clip["reel_output"]`, never `clip["output"]`.
- `render_reels(ws, settings, force=False, clip_ids=None, preset=None,
  dry_run=False, progress=NULL_PROGRESS) -> Path`.
- Only renders clips with status `approved` or `cut` unless explicit
  `clip_ids` are given — "a publishing step, only things a human has blessed."
- Spec resolution order: clip's own `reel` spec → workspace's
  `state.reel_default` → `reelspec.default_spec(settings)`.
- Chat overlay: re-zeroes the clip's chat window onto its own local
  `0..span` timeline; chat offset comes from a per-clip override or
  `state.chat_offset_seconds`. Rendered via `chatrender.render_frames`. On
  the reference channel **~47% of 45s windows have zero chat messages**,
  which is exactly why the default chat mode is `overlay` (float at full
  size) rather than `panel` (reserve a band that would sit empty half the
  time) — see `reelspec.py`.
- Captions overlay: same shape as chat (`_captions_window` mirrors
  `_chat_window`), rendered via `captionrender.render_frames`, but simpler —
  `captions.json` already times against the VOD's own clock (no
  stream-start-anchor correction needed the way chat needs one), so
  `captions.offset` is purely a manual per-clip nudge. `reelspec.
  overlay_input_index(chat_list, caption_list)` is the single source of
  truth both `stages/reel.py` (for where fx asset inputs should start) and
  `reelspec.build_argv` (for the actual `-i` ordering) use to agree on ffmpeg
  input indices — video is always 0, chat (if present) is next, captions (if
  present) after that.
- Speaker avatar overlay: `clipbot.speakers.speaking_spans()` is computed
  once for the whole batch (who spoke when doesn't change per clip), then
  `speakerfx.resolve_speakers(...)` rebases it onto each clip's own
  timeline, the same `origin`/`span` convention `fxspec.resolve_fx` uses.
  Avatar/ring image inputs sit right after chat/captions and before fx's own
  asset inputs — `stages/reel.py` computes fx's `next_input` as
  `overlay_indices["next"] + len(speaker_plan["inputs"])` rather than a
  third slot in `overlay_input_index` itself. **No extra fingerprint
  signature needed** (unlike chat/captions): a speaking span's start/end and
  each avatar/ring's cache-derived file path (see `speakerfx.py`) are both
  baked directly into the filter graph string, so `reel.py`'s
  argv-hash fingerprint already invalidates on any change to who's assigned,
  which avatar image is set, or the ring color — fully self-maintaining,
  the same property `_fingerprint`'s docstring already claims for fx.
- Effects resolved via `fxspec.resolve_fx(..., strict=True)` — a **missing
  library asset fails the render** here (vs. `strict=False` in the
  dashboard's non-rendering `/reel/plan` preview, which only warns so the
  editor can show "missing" mid-edit).
- **Idempotence key is the actual built ffmpeg argv, hashed** — not a
  hand-listed tuple. This is self-maintaining: any change to crop, preset,
  crf, canvas, or the filter builder automatically invalidates the cache.
  (`cut.py`'s hand-listed fingerprint, by contrast, does not.)
- `_maybe_script_filter`: if `-filter_complex` exceeds
  `FILTER_SCRIPT_THRESHOLD = 8000` chars, writes it to
  `logs/fx-<clip_id>.filter` and uses `-filter_complex_script` instead —
  Windows caps command lines at 32767 chars, and a dozen effects with long
  asset paths can approach it. The filter graph's own hash still feeds the
  fingerprint, so a stable filename doesn't defeat cache invalidation.
- `reel_output`: `{file, bytes, duration (fx-adjusted — accounts for freeze
  hold / speed change), width, height, preset, upscale: {game, cam},
  source: "vod", rendered_at, fingerprint}`.
- `preview_command(ws, settings, clip, spec, window=None) -> dict` builds
  the **same** resolve/resolve_fx/build_argv pipeline as the real render,
  but at a small proxy canvas (`reel.preview.canvas`, default `360x640`)
  with fast encoder settings, and only renders the window the effects
  actually touch (padded, capped at `reel.preview.max_seconds=6.0`). This is
  what backs the dashboard's live "Effects" preview.
  **Known loose end**: it never passes `chat_list`/`caption_list`/
  `speaker_plan` — chat, captions and speaker avatars are geometry-only in
  `/reel/plan` and don't actually render in the `/reel/preview` proxy
  despite `reel.preview.include_chat` existing in `settings.json`; that
  setting is unused, same unregistered-feature shape as the `waveform` job
  kind below. The dashboard's live canvas (`reel.js`'s `drawReelPreview`)
  compensates by drawing chat/captions as a labelled reserved-space
  rectangle instead of a facsimile of the real content — **speakers gets no
  such treatment at all**: unlike chat/captions, a speaker slot's position
  depends on which speakers actually talk in a given clip
  (`speaking_spans()`, workspace IO), which `/reel/plan`'s pure-math
  `reelspec.resolve()` has no way to compute, so `plan["speakers"]` on that
  route is just the validated spec block, not resolved slot rects. The
  canvas preview currently shows nothing for the speakers layer at all.
- `unrendered_reels(ws)` mirrors `uncut_approved` — blocks VOD deletion
  while reel-configured clips lack a rendered file (reels re-encode from
  source, so losing the VOD strands them).

## Spec modules (pure math, no IO)

These two modules encode the entire vertical-reel/effects domain model.
Both raise `SpecError` (a `ValueError` subclass from `specerror.py`) on
anything invalid — **this matters because the server's PATCH routes map
`ValueError` → HTTP 400**, so a bad spec reaches the editor UI as a real
error message instead of a silent no-op or a 500.

### reelspec.py — crop geometry, layout, chat placement

- Crop rects are stored as **fractions of the source frame**, never pixels,
  so a spec authored against a 720p VOD still resolves correctly if the
  same channel is later downloaded at 1080p.
- Composition model everywhere: `crop → scale(force_original_aspect_ratio=
  increase) → crop → setsar=1` — guarantees exact target size with no
  distortion regardless of how a dragged rect rounds.
- `DEFAULT_CAM = {x: 0.025, y: 0.30, w: 0.25, h: 0.25}` — measured directly
  off the reference channel's OBS layout (webcam at 32,216 320×180 in a
  1280×720 frame = exactly 25% scale, 32px from the left).
- `PRESETS`: `cam_top`, `cam_bottom`, `blur_fill`, `pip`, `game_only` (the
  same five choices exposed by `clipbot reel --preset`).
- `LAYOUTS = (stacked, blur, full)`, `CAM_EDGES = (top, bottom)`,
  `CHAT_MODES = (panel, overlay)` (only `bottom`/`right` sides allowed, so
  the video's destination box is always anchored at `(0,0)`).
- `CAPTION_POSITIONS = (bottom, top, center)`, always full-width and
  floating (there is no `panel` mode for captions — a clip's speech coverage
  varies far more than chat volume does, so permanently reserving canvas
  space for it would waste a third of the frame on light-narration clips).
  `CAPTION_SIZE_RANGE = (0.05, 0.45)`.
- `SPEAKER_MODES = (appear, discord)`, `SPEAKER_EDGES = (bottom, top)`,
  `SPEAKER_ROSTER_MAX = 8`. `layout_speaker_slots(speakers_plan, canvas,
  active_ids) -> [{speaker_id, x, y, w, h}]` is pure geometry (no IO): an
  explicit `roster` on the spec wins outright (a fixed, on-brand layout
  regardless of who actually talks in a given clip); otherwise slots follow
  `active_ids` in the order given, evenly spaced along `edge`. It's pure
  specifically so it can be unit-tested without a workspace — but *which*
  speakers are active is itself workspace-derived
  (`clipbot.speakers.speaking_spans()`), so this function never runs without
  a caller (`speakerfx.resolve_speakers`, in `stages/reel.py`) supplying
  `active_ids` from outside.
- `normalize(spec) -> dict` validates and raises `SpecError`. Round-trips an
  effects-free spec to exactly its pre-effects shape — `fx`/`chat`/
  `captions`/`speakers` keys are only emitted when non-empty, so adding
  features doesn't churn `clips.json` for clips that don't use them.
- `resolve(spec, src_w, src_h, settings=None) -> plan` turns a validated
  spec + source size into concrete, even, in-bounds pixel rectangles. Used
  identically by the real renderer and the dashboard's non-rendering preview.
  `plan["captions"]["rect"]` is anchored against the **full canvas**, not
  `content`, so it sits above a reserved chat panel band too.
- `overlay_input_index(chat_list, caption_list) -> {"chat", "captions",
  "next"}` is the single source of truth for the ffmpeg input order shared
  between chat and captions: video is always input 0, chat (if present) is
  next, captions (if present) after that, and `"next"` is where the next
  block of inputs should start. `stages/reel.py` extends this one step
  further itself for speakers — `fx_next_input = overlay_indices["next"] +
  len(speaker_plan["inputs"])` — rather than teaching this function a third
  slot, since counting `speaker_plan["inputs"]` after the fact is exact and
  doesn't need its own arithmetic to drift out of sync.
- `build_filter(plan, chat_input=None, fx_plan=None, caption_input=None,
  speaker_plan=None) -> str` dispatches to a **frozen legacy path**
  (`_build_filter_legacy`) only when there's no effects content **and** no
  captions **and** no speakers, or the effects-aware path
  (`_build_filter_fx`) otherwise. **This split is load-bearing**: `reel.py`
  fingerprints the whole argv, so if adding a feature perturbed a single
  character of an unrelated clip's filter string, every existing reel in
  every workspace would silently invalidate and re-render.
  `tests/test_reelspec_invariant.py` pins the exact golden strings for fx;
  `tests/test_captions.py` and `tests/test_speakers.py` pin the equivalent
  invariant for captions and speakers — if you touch `_build_filter_legacy`,
  all three must still pass unchanged. Enabling captions or speakers routes
  through `_build_filter_fx` even with `fx_plan=None` (every
  `fxspec`/`speakerfx` `build_*_chains` helper already no-ops on a falsy
  plan), rather than teaching the frozen legacy path anything new.
- Layer order in the effects-aware path (deliberate, documented per-layer):
  `game+cam compose → camera moves (punch/shake) → chat → captions →
  speaker avatars → stickers → text → flash → freeze hold → format`. Camera
  moves precede chat so a punch-in doesn't zoom the chat box; captions and
  speaker avatars sit directly above chat because all three are the stream
  itself (typed, spoken, and who's-talking respectively), not the edit;
  stickers/text sit above all three because they *are* the edit; flash is
  last of the visible layers because "a flash the title punches through is
  not a flash."
- `build_argv(..., chat_list=None, caption_list=None, speaker_plan=None,
  ...)`: all `-i` inputs must precede `-t` (it becomes an output option only
  once no `-i` follows). Speaker avatar/ring inputs and effect assets are
  both passed as real `-i` argv entries (not `movie=`/`amovie=` filter
  sources) so Windows drive-letter paths need no filtergraph escaping.

### fxspec.py — per-clip effects

- **The single most important invariant**: an effect's `at` is an
  **absolute VOD timestamp** — the same clock as `clip["start"]` and the
  video's `currentTime`, **not** relative to the clip. This is deliberate:
  trimming a clip's in/out point, or globally nudging `cut.pad_start`, must
  never silently slide a placed effect off the moment it was authored
  against. **The one exception is presets** (`library.py`), which store an
  `offset` from an anchor (`"at"`, `"clip_start"`, or `"clip_end"`) because a
  preset is portable by definition.
- `TIMED_TYPES = (punch, shake, flash, sfx, sticker, text)`,
  `WHOLE_TYPES = (freeze, speed, music)`,
  `SINGLETON_TYPES = (speed, music)` (only one per clip — a second would
  silently win). `MAX_EFFECTS = 24`.
- Effect fields (post-`normalize_fx`), see `DEFAULTS`/`RANGES` for exact bounds:
  - `punch`: zoom-in via split+scale+crop+overlay (deliberately **not**
    `zoompan`, which needs an fps that isn't recorded anywhere and re-times
    its own output).
  - `shake`: single shared moving-crop-window expression (not one crop per
    shake), so overlapping shakes add rather than compound. Amplitude is
    authored against a 1080-wide canvas and scaled by `cw/1080.0`, so the
    same spec shakes proportionally at a 360-wide proxy preview and at a
    full 1080×1920 render.
  - `flash`: uses `eq` with `eval=frame` — without it the brightness
    expression evaluates once at filter init and the flash never fires.
  - `freeze`: whole-clip only, holds the last frame via `tpad=stop_mode=
    clone`. A mid-clip freeze would need trim/concat, which re-times
    everything after it — deliberately a different, unbuilt feature.
  - `speed`: whole-clip only, `rate` bounded `[0.5, 2.0]` (a single
    `atempo` instance's range).
  - `sfx`/`music`/`sticker`: reference a library `asset` id
    (`^[a-z]{3}_[0-9a-f]{8,32}$`, matching `library.py`'s `asset_id()`).
    Music ducking under speech via `sidechaincompress` (`DEFAULT_DUCK`:
    threshold 0.05, ratio 8.0, attack 20ms, release 300ms).
  - `text`: max 120 chars, **no straight apostrophe allowed** (ffmpeg
    `drawtext` can't escape one inside a quoted value — must use the
    typographic `’`).
- **`amix=...normalize=0` is load-bearing** — `amix` divides by input count
  by default, which would silently drop the stream's own audio ~10dB the
  moment a music bed is added. Guarded by
  `tests/test_fxspec.py::test_amix_never_normalises`.
- `resolve_fx(fx, origin, span, canvas, assets=None, styles=None,
  next_input=1, strict=True, fallback_font=None) -> plan | None`. `strict`
  controls whether a missing asset raises (`SpecError`, real render) or
  warns (dashboard's non-rendering plan preview). Returns `None` if nothing
  survives, signaling callers to take the frozen effects-free path.
  `out_duration = (span + hold) / rate` — freeze lengthens output, speed
  changes shorten it; the final `-t` must use this, never the raw `span`.
- `escape_path`/`escape_text` are the only two escaping functions, used
  respectively for `drawtext`'s `fontfile=` and literal drawn text.

## Library system (clipbot/library.py)

- Root: `library.dir` setting, else `<work_root>/_library` (a sibling of
  `_cache`, both underscore-prefixed so they're excluded from the workspace
  listing and rejected by the server's slug validator even though the slug
  regex would otherwise admit them).
- **Content-addressed asset ids**: `asset_id(kind, sha) =
  "{prefix}_{sha1[:16]}"`, prefix ∈ `{sfx, mus, stk, fnt, avt}` (`avt` =
  avatar, static images only - `.png/.jpg/.jpeg/.webp`, no animated ones;
  the "moving" part of a speaker overlay is the ring lighting up, not the
  source image). Deliberate: an id never changes when a file is renamed or
  moved, so a clip that references it keeps working.
- `scan(settings, prune=False) -> index`: incremental — only re-hashes/
  re-probes files whose `(size, mtime_ns)` changed since the last scan.
  Audio probed via ffprobe (`duration`, `sample_rate`, `channels`); images
  via Pillow (`width`, `height`, `animated`).
- **30-day deletion grace period** (`TOMBSTONE_DAYS = 30`, configurable via
  `library.missing_tombstone_days`): a file whose `rel` path and `id` both
  vanish from a scan becomes a tombstone kept in `index.json["missing"]`
  until it ages out (or `prune=True`) — this is what lets the review UI say
  "this sound is missing" instead of an effect silently disappearing.
- `meta.json` is a **hand-editable overlay** applied on top of scan results:
  renames and normalized tags. The files on disk are the truth; `index.json`
  is a disposable cache.
- **Presets** (`save_preset`/`list_presets`/`delete_preset`): `{version, id,
  name, anchor, requires: [asset_ids], effects: [...], created_at,
  updated_at}`. Effect entries store an `offset` from the anchor instead of
  `at`. Every preset is round-tripped through the real `fxspec.normalize_fx`
  validator (with a placeholder `at=1000.0`) before being saved — guarantees
  a preset can never contain something a clip would later reject.
- `fetch_starter()` downloads the CC0 starter pack named in
  `config/library_starter.json` (which **ships with an empty `assets: []`
  on purpose** — Pixabay/Freesound hand out download URLs from client-side
  API calls that can't be durably captured into a manifest). Hard host
  allowlist (`library.fetch.allow_hosts`, checks the final redirect target
  too) plus a required `sha256` per entry — **the manifest is treated as
  data, not trusted config**.
- **Speaker profiles** (`list_speakers`/`save_speaker`/`delete_speaker`,
  `speakers.json`): `{version, speakers: [{id, name, avatar_asset, color,
  style, created_at, updated_at}]}`. Unlike assets, a speaker's `id` is
  `slugify_name(name)` (same scheme `save_preset` already uses), not
  content-addressed — a profile isn't immutable file content the way a
  sound or sticker is. `avatar_asset` references an `avt_...` library asset
  id; `color` (`#RRGGBB`) drives the "discord mode" ring in
  `speakerfx.py`. Library-level, not per-workspace, for the same reason as
  every other asset kind: a recurring co-host should look the same in every
  VOD.

## Chat sync (clipbot/chatsync.py)

- The problem: chat message offsets are stored relative to when Kick
  *opened the livestream record*; clip `start`/`end` are VOD-time (relative
  to the recording's own frame 0). These clocks only agree if recording
  began the instant the stream record opened — measurably, on the reference
  channel, it does not (~26.6s later).
- `estimate_offset(ws, chat_doc, settings) -> dict`: cross-correlates a
  binned audio-energy envelope (read directly from `audio.wav`, no
  intermediate waveform file — see "Known loose ends" below) against a
  binned chat-message histogram. `confident = z_score >= 3.0`.
- **Quarter-split cross-check**: independently correlates each quarter of
  the VOD's audio span; if the four estimates disagree by more than
  `3 * bin_seconds`, flags `nonlinear_warning` suggesting a per-clip
  `chat.offset` override (catches a mid-stream reconnect).
- **Boundary heuristic fallback**: `boundary_offset_seconds = messages[-1].offset
  - duration` ("last message vs. VOD end"). Exists because correlation
  performs badly on genuinely sparse chat (measured: 358 messages over 4.6h
  scores z=2.2, below the confidence gate) but a streamer signing off is a
  reliable, tighter signal on thin data. `clipbot chat-sync --apply` (and
  the dashboard's `/chat/sync` route) prefer the boundary estimate
  specifically when `confident` is `False`.

## Speaker assignment (clipbot/speakers.py)

- Mirrors `review.py`'s sidecar-over-immutable-artifact pattern:
  `transcribe.py` stays the sole writer of `transcript.json` and
  `diarize.py` stays the sole writer of `diarization.json`; this module only
  reads those two and writes the assignment overlay, `speaker_map.json`, on
  top - `{version, updated_at, assignments: {"<segment id>": "<speaker id>"}}`.
- A value in `assignments` is either a raw diarization cluster label
  (`SPEAKER_00`, before naming) or a `library.py` `speakers.json` id (after
  naming/merging) - this module doesn't care which; the dashboard's "name
  this cluster" action is what rewrites matching values from one to the
  other in bulk, via `rename_speaker(ws, old_id, new_id)`.
- `assign_range(ws, start_id, end_id, speaker_id)`: the manual-override path
  (`speaker_id=None` clears instead of assigns) - and the *only* path at all
  for a workspace that never ran `diarize`.
- `bulk_from_diarization(ws, transcript_doc, diarization_doc, overwrite=False)`:
  assigns each transcript segment the diarization turn with the largest time
  overlap. Defaults to filling in only unassigned segments, so re-running
  diarization can't silently discard a human's manual corrections or
  already-named speakers; `overwrite=True` is the explicit "redo from
  scratch" action.
- `resolved_segments(transcript_doc, map_doc)` and `speaking_spans(segments,
  max_gap=2.0)` are the shared query functions every downstream consumer
  uses (the dashboard's transcript view, `stages/reel.py`'s avatar overlay).
  `speaking_spans` collapses consecutive same-speaker segments into spans,
  dropping unassigned ones entirely; a gap longer than `max_gap` between two
  segments from the same speaker starts a new span rather than merging
  through a pause.

## The web dashboard (clipbot/server/)

- **FastAPI, not Flask.** `docs_url=None, redoc_url=None` — Swagger/Redoc
  explicitly disabled. Static files at `/static`, Jinja2 templates from
  `templates/`.
- **Security posture: no auth, loopback-only, by design.**
  `server.host = 127.0.0.1`. A `guard()` middleware rejects any request
  whose `Host` header isn't `127.0.0.1`/`localhost`/`[::1]` (blocks DNS
  rebinding), and rejects any non-GET/HEAD/OPTIONS request that lacks a
  custom `X-ClipBot: 1` header — a cross-origin page can't set a custom
  header without triggering a CORS preflight, and the server sends no CORS
  headers, so this blocks CSRF-style mutation from another origin.
  `static/app.js`'s `api()` helper adds that header on every non-GET call.
  **Do not expose this server beyond loopback without adding real auth.**
- `get_workspace(slug)` validates against `SLUG_RE = ^[a-z0-9._-]{1,120}$`,
  rejects slugs starting with `_` (reserved for `_cache`/`_library`), and
  confirms the resolved path doesn't escape `work_root` (path traversal guard).
- Route groups (see `app.py` for the full list): pages (`/`, `/w/{slug}`,
  `/w/{slug}/review`); workspaces (`GET/POST /api/workspaces`); clips
  (`GET/POST /api/workspaces/{slug}/clips`, **`PATCH .../clips/{clip_id}`**
  — this is the route `specerror.py` references: it catches `KeyError` →
  404 and `ValueError` → 400, so a bad reel/effects spec surfaces as a
  clean 400 with the validator's message); transcript; chat (`sync`,
  `offset`, `messages`); speakers (`GET .../speakers` — transcript segments
  merged with their assigned speaker, for the transcript view;
  `POST .../speakers/assign` — manual range override, `speaker_id: null`
  clears; `POST .../speakers/rename` — bulk-rewrite every assignment
  pointing at one id to another, the "name this diarization cluster"
  action); reel editing (`reel/plan` — pure math, no ffmpeg, what the
  browser preview draws from; `reel/apply`; `reel/preview[/cancel]` — the
  real filter-graph proxy render, though see `preview_command`'s note in
  reel.py's section above for what it still doesn't render); library
  (assets, presets, text styles,
  speaker profiles — `GET/POST /api/library/speakers`,
  `DELETE .../speakers/{id}` — an audition media route); jobs
  (`POST .../jobs`, `GET /api/jobs`, cancel); `GET /api/doctor`
  (diagnostics: tool resolvability, API key presence, CUDA device count,
  free disk, Python version); media (video/clip/reel serving with
  `?download=1`, manifest CSV download); and `GET /api/events`
  (Server-Sent Events, with `Last-Event-ID` replay).
- `external_activity(ws)` detects a **CLI-driven** job the dashboard didn't
  start itself: partial download files (`.part`/`.ytdl`), or (Windows-only)
  a `wmic process ... get commandline` scan matching `clipbot` + the
  workspace's video id/slug — needed for stages like transcribe that write
  no partial file to detect otherwise.

### jobs.py — JobRunner / EventBus

- **Two worker lanes**: `HEAVY_KINDS = (download, audio, transcribe,
  analyze, diarize, pipeline, reel)`, `LIGHT_KINDS = (cut, chat,
  transliterate, manifest, cleanup, waveform, benchmark)`, each on its own
  thread — a 3-second clip cut never queues behind a 90-minute transcribe,
  and chat harvest (must run before Kick's retention expires it) never
  queues behind an x264 render. `transliterate` is light for the same
  reason as `chat`: a handful of batched Claude calls, not CPU/GPU-bound.
  `diarize` is heavy for the same reason as `transcribe`: CPU-bound (no CUDA
  on this machine) and can run long on a multi-hour VOD.
- Threads, not subprocesses, because none of the actual work is
  Python-CPU-bound (CTranslate2 releases the GIL; ffmpeg/yt-dlp are
  subprocesses; Claude calls are network I/O) and the ~3GB Whisper model can
  stay resident between runs.
- `Job` states: `queued → running → {succeeded, failed, cancelled,
  interrupted}`. **One job per workspace at a time**, enforced at
  `submit()`, to prevent concurrent `state.json` writes.
- `EventBus` keeps a `deque(maxlen=400)` history with monotonic ids for SSE
  `Last-Event-ID` replay on a reconnecting browser tab.

### Known loose end: `waveform` job kind is unregistered

`jobs.py`'s `LIGHT_KINDS` lists `"waveform"`, and `settings.json` has a
`server.waveform_bins_per_second` setting, but `app.py` never registers a
`waveform` handler — submitting one raises `StageError("Unknown job kind
'waveform'")`. `chatsync.py` reads `audio.wav` directly instead of a
pre-computed `waveform.json`. This looks like a partially-built/abandoned
feature — don't assume a waveform file exists anywhere.

## Effects/reel preview pipeline (clipbot/preview.py)

- `PreviewRunner` is a **separate**, single-worker job system from
  `JobRunner` — deliberately, because `JobRunner.submit` would either reject
  a preview while a real reel render is queued, or occupy the workspace's
  only slot and block a real render behind a throwaway preview. Previews
  also need "supersede" semantics (a slider move makes an in-flight render
  worthless) that `JobRunner` has no concept of.
- Design constraint: the proxy differs from the final render **only** in
  canvas size and encoder settings — the effects themselves are never
  approximated, so "a preview that looks right cannot be followed by a
  render that doesn't."
- Dedupes by fingerprint key (`pv_<hash>`); a new key for the same workspace
  cancels any other in-flight preview for that workspace. 60-second
  watchdog force-cancel. LRU-ish eviction to `reel.preview.cache_max_mb`
  (512MB default).

## Configuration reference (config/settings.json)

Every leaf is overridable by a `CLIPBOT_*` env var — see `ENV_OVERRIDES` in
`clipbot/config.py` for the exact list (work root, library dir, tool paths,
whisper model/device/language, Claude model, rubric file path).

Sections and the values worth knowing without opening the file:

- **`tools`** — `ffmpeg`/`ffprobe` point at the **vendored** local build
  (`ffmpeg-2026-07-30-git-.../bin/`) because ffmpeg isn't on this machine's
  PATH; relative paths resolve against the project root.
- **`download`** — `format` pins `height<=720` explicitly ("best" once
  silently returned 160p); `fallback_to_kick_dl: false` (see above).
- **`chat`** — bot list matched on `sender.slug`, lowercase; full
  `chatrender` style block (fonts, sizes, colors) lives here too.
- **`transcribe`** — `language: "hi"` (forced, not auto-detect);
  `low_confidence_threshold: -0.7`; `max_segment_seconds: 30.0`.
- **`analyze`** — `model: "claude-sonnet-4-6"`; `thinking: false` (see
  analyze.py above for why); `effort: "medium"`.
- **`transliterate`** — `model: "claude-haiku-4-5"` (deliberately cheaper
  than `analyze.model` — mechanical task, not a judgment call);
  `batch_size: 50`; `thinking: false`.
- **`diarize`** — `model: "pyannote/speaker-diarization-3.1"`,
  `device: "auto"` (same cuda-else-cpu resolution as `transcribe.device`),
  `hf_token_env: "HF_TOKEN"` (names the env var, not the token itself);
  `num_speakers`/`min_speakers`/`max_speakers` all `null` by default (let
  pyannote infer).
- **`cut`** — `pad_start: 1.0`, `pad_end: 1.5`, `re_encode: false`.
- **`reel`** — `canvas: "1080x1920"`, `preset: "cam_top"`; `preset_x264:
  "slow"` **deliberately does not inherit `cut.preset`** (measured: 30s of
  1080×1920@60 is 6s at `veryfast` vs 19s at `slow` — "quality is worth the
  19s"); nested `fx.*` block mirrors `fxspec.DEFAULTS`; nested `captions.*`
  block is `captionrender.resolve_style`'s fallback (font/size/colors) — a
  clip's `position`/`size`/`max_lines`/`offset` live on the per-clip spec
  instead, same split as chat's style-here/placement-on-the-clip pattern;
  nested `speakers.*` block is the same kind of fallback for `mode`/`edge`/
  `avatar_size`/`gap`/`ring_width_px` (a clip's `roster` override, if any,
  only ever lives on the per-clip spec — there's no sensible workspace-wide
  default for "who's pinned to which slot"); nested `preview.*` block
  controls the proxy renderer.
- **`library`** — `dir: ""` (defaults to `<work_root>/_library`);
  `fetch.allow_hosts` is the hard CDN allowlist.
- **`server`** — `host: 127.0.0.1`, `port: 8765`, no auth (see security
  posture above).

## Testing

- `tests/test_fxspec.py` — effect schema validation (range checks,
  singleton violations, duplicate ids, the straight-apostrophe rejection,
  asset-id shape), escaping correctness, absolute-time resolution (the
  in-point-nudge invariant is tested directly), missing-asset strict-vs-warn
  behavior, and filter-string shape (quoting, `eval=frame`, `amix
  normalize=0`).
- `tests/test_reelspec_invariant.py` — pins that adding effects support
  never perturbed the effects-free filter graph, since `reel.py` fingerprints
  the whole argv and any drift would silently trigger a mass re-render.
  Includes literal golden filter-graph strings for two preset/chat
  combinations — if `_build_filter_legacy` changes, this test must still
  pass unmodified, or the golden strings need a deliberate, reasoned update.
- `tests/test_captions.py` — the same legacy-path-unaffected discipline
  applied to the captions overlay (a captions-off clip's `build_filter`/
  `build_argv` output must be identical to before the feature existed), plus
  `_captions_block` validation, `resolve()`'s rect geometry for each
  `CAPTION_POSITIONS` value, and `captionrender.render_frames` producing
  real frames + a valid ffconcat list for a couple of segments.
- `tests/test_speakers.py` — the same legacy-path-unaffected discipline
  applied to the speakers overlay; `clipbot.speakers`' pure merge functions
  (`resolved_segments`, `speaking_spans` including the gap-splitting
  behaviour) plus its workspace-backed functions (`assign_range`,
  `rename_speaker`, `bulk_from_diarization`'s never-overwrite-by-default
  behaviour) against a real temp `Workspace`; `_speakers_block` validation
  and `layout_speaker_slots` geometry; and `speakerfx.resolve_speakers`
  against a real tiny Pillow-generated source image, covering appear vs.
  discord mode and the strict/non-strict missing-avatar behaviour.
- Run with `python -m unittest discover -s tests` (no `pytest` installed in
  this environment as of this writing; no CI config in this repo either).

## Cross-cutting invariants worth knowing before you touch things

- **Low-confidence transcript segments are flagged, never dropped**, at
  every layer (transcribe.py, the rubric, chat rendering). Don't add code
  that silently filters them.
- **Effect/annotation times are absolute VOD seconds**, not clip-relative,
  except inside a saved library preset (which stores an anchor + offset).
  Don't "simplify" this to clip-relative without re-deriving why it's not.
- **`SpecError` must stay a `ValueError` subclass** — the server's PATCH
  error handling depends on it.
- **The effects-free, chat-off, captions-off, speakers-off reel filter
  graph must stay byte-identical** to what `test_reelspec_invariant.py`
  pins, because `reel.py`'s cache fingerprint hashes the whole argv. A new
  overlay layer (captions, speakers, and any future one) must route through
  `_build_filter_fx` rather than touching `_build_filter_legacy`, same as
  `fx` already does.
- **`amix` in the audio effects chain must always carry `normalize=0`.**
- **`-ss` goes before `-i` for stream-copy cuts**; an output seek silently
  decodes everything before the cut point.
- **Never read `state.duration` from anything but the audio-stage probe.**
  `kick_duration` from yt-dlp metadata is informational only and is never
  allowed to overwrite it.
- **`ANTHROPIC_API_KEY` must be set** for `analyze` and `transliterate`;
  there's no fallback path for either.
- **`diarize` needs both `requirements-diarize.txt` installed and an HF
  token** (`diarize.hf_token_env`, default `HF_TOKEN`) with the pyannote
  model's user agreement accepted on Hugging Face's site — none of this is
  checked until the stage actually runs, so a workspace can sit at "ready"
  in the dashboard and still fail immediately with a clear error, same
  UX as `analyze` without `ANTHROPIC_API_KEY`.
- **This machine has no usable transcription GPU** (CTranslate2 has no
  ROCm backend) — `transcribe.device: auto` always lands on `cpu` here.
  `scripts/setup-pc2.ps1` provisions a second, CUDA-capable machine for
  faster transcription; if you're working on that machine, `transcribe.device`
  will resolve to `cuda` instead.
- **The dashboard has no authentication and binds loopback only** — treat
  any change that widens `server.host` or adds routes as a security-relevant
  change, not a routine one.
- **Python 3.9 is the floor** (`requirements-server.txt`'s version ceilings
  exist solely to stay 3.9-compatible); 3.10+ is recommended but not required.

## Quick index — "I want to..."

- ...add a new effect type → `clipbot/fxspec.py` (schema + filter builder)
  + `clipbot/reelspec.py` (layer ordering in `_build_filter_fx`) +
  `config/settings.json` `reel.fx.*` defaults + `tests/test_fxspec.py`.
- ...add a new reel layout preset → `clipbot/reelspec.py` `PRESETS` +
  `clipbot/cli.py`'s `--preset` choices + `server/static/reel.js`.
- ...change how clips are picked → `config/rubric.md` (no code change
  needed) or `config/analysis_prompt.md` (keep `{{PLACEHOLDERS}}` and the
  JSON schema intact).
- ...swap the transcription backend → write a new module that emits the
  same `transcript.json` shape as `stages/transcribe.py`; nothing else needs
  to change.
- ...add a new CLI subcommand → `clipbot/cli.py`'s `build_parser()` +
  a `cmd_*` function; add a matching dashboard job handler in
  `server/app.py` only if the dashboard should expose it too.
- ...add a new dashboard route → `clipbot/server/app.py`; if it mutates
  state, raise `ValueError`/`KeyError` for expected failure modes (mapped to
  400/404) rather than letting a 500 leak.
- ...add a new stream-derived overlay layer (like captions or speakers,
  something *derived from the stream*, not manually placed like an fx
  effect) → a validator in `clipbot/reelspec.py`'s `normalize()` (only
  emitted when non-empty) + a rect/slot resolver (in `resolve()` if pure
  geometry suffices, or a real render-tier module like `speakerfx.py` if it
  needs IO) + a new layer call in `_build_filter_fx` (**never**
  `_build_filter_legacy`) + wiring in `stages/reel.py` to gather the
  workspace data, compute the fingerprint-relevant signature if the layer
  isn't self-maintaining via the argv hash (see `speakerfx`'s docstring for
  when it is), and pass the resolved plan through `build_argv`.
- ...understand a workspace's on-disk state → `clipbot/workspace.py`'s path
  properties are the authoritative list; `state.json`'s `stages` dict says
  what's been completed.
- ...understand the review/approval state machine → `clipbot/review.py`
  (`reconcile()` is the interesting one: it re-matches a fresh rubric run's
  candidates against existing human-edited clips by time-overlap).

## Keeping this file honest

There's a project skill, `clipbot-docs-sync`
(`.claude/skills/clipbot-docs-sync/SKILL.md`), whose job is to update this
file whenever a change to the code would make something written here wrong.
If you make a change that touches anything documented above (a new stage, a
new route, a changed schema field, a changed threshold/constant, a new
settings key, a new job kind) and you're not already running that skill,
invoke it (or at minimum, hand-edit the relevant section here) before
calling the change done — this file is only useful if it doesn't lie.
