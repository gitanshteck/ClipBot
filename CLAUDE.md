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
> 2026-10-20.

## What this actually is, right now

ClipBot turns a Kick or YouTube livestream VOD into reviewed, cut clips
(vertical-ready reels are Kick-only). The README's old top banner ("stages 1–2
built... transcription/analysis/cutting scaffolded but not implemented") has
since been corrected, but its pipeline table still lists only the main path.
The real state:

- All 6 pipeline stages (download, audio, transcribe, analyze, cut, cleanup)
  are fully implemented, plus a chat-harvest stage (1b), an opt-in speaker
  diarization stage (2b), a Hinglish-caption transliteration stage (3c), a
  reel-render stage (5b), and a compile stage (5c) — assembling a named list
  of non-contiguous VOD ranges into one landscape supercut — none of which
  the README's table even lists.
- There is a full local web dashboard/editor (`clipbot/server/`, FastAPI) —
  not just a CLI. It's the primary way clips actually get reviewed, cropped,
  effect-decorated, and re-rendered as 9:16 reels.
- There's a cross-stream asset library (`clipbot/library.py`) for sound
  effects, music, stickers, fonts, and saved effect presets.
- **YouTube is a second platform**, built so a stream never has to be
  downloaded: a YouTube workspace fetches **audio only** (`stages/youtube.py`)
  and the dashboard plays the **embedded YouTube player** (`static/player.js`)
  instead of a local `<video>`. Built: the platform model
  (`clipbot/platforms.py`), audio-only ingest, the Kick | YouTube dashboard
  tabs, the embedded player on the review and compile pages, and cutting
  clips / compilation segments **straight from YouTube**
  (`clipbot/ytsegments.py`: only the requested seconds are fetched, always
  re-encoded). **Not built**: YouTube chat replay, a full-video download for
  YouTube, and a stream-copy ("fast") cut/compile mode - measured and
  deliberately not offered, see the ytsegments.py section. **Reels are a
  deliberate non-goal for YouTube**:
  the reel editor paints video frames into a canvas
  (`static/reel.js`'s `drawReelPreview`), which a cross-origin iframe can never
  provide, so embed mode doesn't render the reel panel, crop layer or effects
  lane at all.
- **Publishing a Kick VOD to YouTube** (`clipbot upload`, stage 6a): the VOD is
  uploaded to the user's channel from disk, or relayed straight from Kick with
  **no download** (`stages/upload.py`, `ytupload.py`, `youtube_auth.py`).
  Opt-in - never part of `run`/`pipeline` - and it needs an OAuth client the
  user creates in Google Cloud; nothing ships with the repo. **Not built**:
  uploading clips, reels or compilations (the resumable client is generic, only
  the VOD source selection exists), editing metadata after upload, scheduling.
- **An edit engine** (`clipbot edit ...`, `clipbot/edit*.py`, `stages/edit.py`):
  a declarative *edit spec* -> a finished multi-source video (YouTube + Kick +
  files) with speed ramps, camera moves, grade, callouts, transitions, music and
  sound effects, loudness mastering, self-inspection (`edit verify`) and a
  delivery package (`edit deliver`). It is how compilations beyond a plain concat
  get made; `compile` stays the simple path. Documented in "The edit engine".
- **A kill-feed reader** (`clipbot killfeed scan`, `stages/killfeed.py`,
  `killfeed.py`): OCRs a shooter's on-screen kill feed (Windows OCR) and writes
  `killfeed.json`, the data source for `edittemplates.kill_montage`. Tuned on
  Wardogs; see "killfeed.py (stage 2c)".
- Everything below reflects the code as it stands, not the README's table.

The README (setup instructions, the "YouTube", "Transcription", "Analysis",
"Editing library" sections) is accurate and up to date; only its opening
pipeline table is incomplete.

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
  `work/<slug>/` - `<channel>-<video-id>` for Kick, `yt-<lowercased video id>`
  for YouTube (`platforms.slug_for`). Knows its own `platform` (`state.json`
  key; absent means Kick, since every workspace that predates YouTube is one),
  `video_id`, and `source_mode()` - `local` (a video file is on disk),
  `embed` (YouTube workspace with none), or `none` - **derived from what is on
  disk, never stored**, the same rule the dashboard's stage list follows. Owns
  path properties (`audio_path`,
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
                     compile, manifest, upload, youtube-auth, cleanup, info, list,
                     library {scan,list,presets,licenses,hash,fetch,path},
                     edit {new,validate,inspect,render,verify,deliver,list,clean,taste},
                     killfeed {scan,list}) - the last two groups are registered
                     by editcli.add_parsers(), not defined in cli.py
  __main__.py        `python -m clipbot` -> cli.main()
  config.py          Settings / load_settings() / ENV_OVERRIDES
  workspace.py        Workspace (platform / video_id / source_mode() /
                     source_audio_path()), slug_for_url() (delegates to platforms.py)
  platforms.py       parse_url() / slug_for() / platform_of(): recognises Kick and
                     YouTube video URLs, derives the workspace slug + canonical URL.
                     The Kick regex lives here verbatim - Kick slugs are directory
                     names and must never change (tests/test_platforms.py pins them
                     against a copy of the pre-YouTube implementation)
  ytdlp.py           run_yt_dlp() / parse_progress_line(): the shared yt-dlp
                     subprocess runner (byte progress + cancellation), used by
                     stages/download.py (Kick) and stages/youtube.py
  ytsegments.py      cutting a time range straight out of a YouTube video:
                     resolve_streams() / StreamResolver / fetch_segment() -
                     what cut.py and compile.py use when a workspace has no
                     video on disk. Read its section below before touching it
  youtube_auth.py    OAuth for uploading (loopback + PKCE, stdlib only):
                     load_client() / authorize() / TokenSource / status().
                     The client JSON is user-supplied and gitignored
  ytupload.py        YouTube resumable-upload client over http.client:
                     ResumableUpload (upload_file / upload_stream),
                     ChunkReader, build_metadata(). Knows nothing of Kick,
                     ffmpeg or workspaces. Read its section below first
  utils.py           logging setup, run_command() subprocess wrapper, slugify(),
                     format_timestamp(), StageError/ToolMissingError
  specerror.py       SpecError(ValueError) — shared by reelspec.py and fxspec.py
                     on purpose, to avoid a circular import; ValueError-ness is
                     load-bearing (server PATCH routes map it to HTTP 400)
  manifest.py        write_manifest() -> manifest.json + manifest.csv (utf-8-sig)
  review.py          clips.json state machine: reconcile(), update_clip(),
                     add_manual_clip(), clips_for_cutting(), counts()
  compilations.py    compilations.json sidecar for named multi-range
                     supercuts: load()/save()/upsert()/set_output()/
                     add_segment()/delete() - kept separate from review.py
                     on purpose (see stages/compile.py)
  reelspec.py        vertical-reel geometry: normalize()/resolve()/build_filter()/
                     build_argv() — pure math, no IO
  fxspec.py          per-clip effects schema + ffmpeg filter builders — pure
                     math, no IO
  editspec.py        edit-engine spec: normalize()/load()/to_json(), PROFILES,
                     OUTPUT_PRESETS, EASES - pure, SpecError on anything invalid
  edittimeline.py    compile_timeline(spec): retiming pieces, anchors resolved
                     to output time, events, pacing(), chapters() - pure
  editfilters.py     every ffmpeg filtergraph fragment the edit engine emits
                     (retime, canvas, grade, camera, overlays, assembly, mix,
                     master) - strings only, no IO
  editoverlay.py     callout/text/image overlays -> RGBA PNGs (Pillow)
  editaudio.py       numpy audio analysis: peak alignment for SFX, beat grid,
                     join-click score
  editqa.py          verify(): the checks run on a render + storyboard and
                     loudness-plot artefacts
  editdeliver.py     delivery package: platform variants, thumbnails, chapters,
                     post text, manifest
  edittemplates.py   kill_montage(): killfeed events -> a first-draft edit spec
  edittaste.py       the user's standing editing preferences (taste.json)
  editcli.py         the `edit` and `killfeed` command groups
  killfeed.py        kill-feed parsing (pure): rows, distance tags, icons
  data/windows_ocr.ps1  the Windows OCR driver stages/killfeed.py shells out to
  library.py         cross-stream asset library: scan(), asset_id(), presets,
                     speaker profiles, fetch_starter()
  speakers.py        per-workspace segment-id -> speaker-id overlay
                     (speaker_map.json): assign_range(), bulk_from_diarization(),
                     resolved_segments(), speaking_spans()
  speakerfx.py       speaker avatar/ring overlay: resolve_speakers() + the
                     filter-chain builder - NOT pure like reelspec/fxspec,
                     since it synthesizes circle-cropped avatar and ring PNGs
                     via Pillow (cached in _cache/avatars/). Its
                     resolve_speaker_slots() is the one exception - the pure,
                     no-IO geometry prefix of resolve_speakers(), split out
                     so api_reel_plan can call it for the dashboard's canvas
                     preview without paying for image generation
  chatrender.py      chat messages -> PNG overlay frames (Pillow, not libass)
  captionrender.py   Hinglish caption segments -> PNG overlay frames (Pillow;
                     smaller mirror of chatrender.py, no emote/badge images)
  chatsync.py        estimate_offset() — correlate chat rate against audio
                     energy to find chat_offset_seconds
  highlights.py      notable_moments() — energy/chat-activity spike detection
                     analyze.py annotates onto the transcript it sends Claude
  ffrun.py           shared ffmpeg subprocess runner with progress + cancellation
  preview.py         PreviewRunner — the dashboard's live "Effects" proxy preview
  progress.py        Progress / NULL_PROGRESS — CLI-safe, dashboard-aware progress
                     reporting with cancellation and ETA
  stages/
    download.py       stage 1  (Kick: yt-dlp; kick-dl fallback currently disabled)
                       + acquire(), the platform dispatcher the CLI and jobs call
    youtube.py        stage 1 for a YouTube workspace: audio-only download,
                       metadata, channel listing, Doctor health checks
    chat.py            stage 1b (Kick chat REST API harvest)
    audio.py          stage 2  (ffmpeg -vn to 16kHz mono PCM)
    diarize.py         stage 2b (opt-in: pyannote speaker diarization)
    transcribe.py      stage 3  (dispatcher: local faster-whisper or OpenAI API)
    transcribe_openai.py  stage 3 alt backend (OpenAI whisper-1, default; chunked+parallel)
    transliterate.py   stage 3c (Claude API, Devanagari -> Hinglish captions)
    analyze.py         stage 4  (Claude API, prompt caching, JSON extraction)
    cut.py             stage 5  (ffmpeg cut, copy or re-encode)
    reel.py            stage 5b (vertical 9:16 re-encode through fx filter graph)
    compile.py         stage 5c (landscape supercut: cut + concat named ranges)
    edit.py            stage 5d (the edit engine's renderer: spec -> mastered video)
    killfeed.py        stage 2c (OCR a stream's kill feed -> killfeed.json)
    upload.py          stage 6a (Kick VOD -> YouTube: from disk, or relayed
                       from Kick with no download)
  server/
    app.py            FastAPI app, all HTTP routes, security guard()
    jobs.py           JobRunner (3 worker lanes) + EventBus (SSE)
    media.py          hand-rolled HTTP Range file serving
    __main__.py       `python -m clipbot.server` entry point (uvicorn)
    templates/        Jinja2: base.html (topbar with the Kick | YouTube tabs),
                       library.html (the Kick tab), library_youtube.html (the
                       YouTube tab), workspace.html, review.html, compile.html
    static/           vanilla JS/CSS: app.js, player.js (player adapter: a native
                       <video> or the embedded YouTube player), fx.js, reel.js,
                       review.js, compile.js, *.css
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
  test_compile.py               compilations.json CRUD, segment/compilation
                                fingerprinting, ffconcat list shape, and
                                skip-unchanged/force behavior (ffmpeg calls
                                stubbed - see the stage-by-stage section)
  test_platforms.py             URL parsing/slugs (Kick slugs pinned against the
                                pre-YouTube implementation), Workspace platform
                                + source_mode + source_audio_path
  test_youtube.py               stages/youtube.py + ytdlp.py + the audio-stage
                                handoff + acquire() (yt-dlp itself never run,
                                except a real-subprocess test of the runner)
  test_youtube_segments.py      ytsegments.py + its use by cut.py/compile.py
                                (yt-dlp and ffmpeg stubbed): the bounded-request
                                argv, retry, fingerprints, mixed-source rules
  test_youtube_upload.py        ytupload.py + stages/upload.py against an
                                in-process fake of YouTube's resumable protocol
                                (fault injection), plus a real-ffmpeg HLS relay
                                test (skipped without ffmpeg; CLIPBOT_FFMPEG)
  test_youtube_auth.py          youtube_auth.py: PKCE, the loopback flow against
                                a fake token endpoint, refresh, status
  test_upload_server.py         the upload job lane, stage row, Doctor row and
                                "To YouTube" create-workspace path (FastAPI
                                TestClient, no startup hooks)
  test_utils.py                 resolve_tool() incl. values that can't be paths
  test_editspec.py ... test_editcli.py   the edit engine, one file per module -
                                see "Testing"
  test_killfeed.py              kill-feed parsing rules + icon geometry
  test_player_js.py             runs tests/js/player_adapter.test.js under Node
                                (skipped without Node); no browser test
                                infrastructure exists in this repo

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
| 1 | download (Kick) | `stages/download.py` | `video.<ext>` | yt-dlp + `--impersonate chrome`; kick-dl fallback wired but disabled (non-functional TUI). Reached through `download.acquire()` |
| 1 | download (YouTube) | `stages/youtube.py` | `source_audio.<ext>` (+ `video.info.json`) | **audio only** - no video is downloaded; the file is deleted once `audio.wav` exists. Needs a current yt-dlp + a JS runtime (see the youtube.py section) |
| 1b | chat | `stages/chat.py` | `chat.json` | **Kick only** (YouTube has no chat stage yet); Kick REST API, cursor pagination; best-effort, never blocks the rest of `run` |
| 2 | audio | `stages/audio.py` | `audio.wav` | ffmpeg `-vn`, 16kHz mono PCM; reads `source_audio.*` first (YouTube), then the VOD |
| 2b | diarize | `stages/diarize.py` | `diarization.json` | pyannote, CPU-only here; opt-in, needs `requirements-diarize.txt` + an HF token, never part of `run` |
| 2c | killfeed | `stages/killfeed.py` | `killfeed.json`, `killfeed_icons/` | OCRs the shooter's kill feed for one player's kills (Windows OCR); opt-in, never part of `run`. A YouTube workspace's video track is fetched to scratch and deleted |
| 3 | transcribe | `stages/transcribe.py` (dispatcher) → `stages/transcribe_openai.py` (default) or local faster-whisper | `transcript.json` | `transcribe.backend` picks OpenAI whisper-1 (default, chunked+parallel, ~$0.36/hour) or local faster-whisper `large-v3`; flags but never drops low-confidence segments either way |
| 3c | transliterate | `stages/transliterate.py` | `captions.json` | Claude API, batched; Devanagari transcript -> Hinglish (Latin script) for caption overlays; opt-in, never part of `run` |
| 4 | analyze | `stages/analyze.py` | `candidates.json` | Claude API against the rubric; structural validation only |
| 5 | cut | `stages/cut.py` | `clips/*.mp4` | ffmpeg copy (default) or re-encode; writes into `clips.json[*].output`. A YouTube workspace (no video on disk) is cut straight from YouTube via `ytsegments.py`, always re-encoded |
| 5b | reel | `stages/reel.py` | `clips/reels/*.mp4` | vertical 9:16 re-encode through the fx/chat/captions/speakers filter graph |
| 5c | compile | `stages/compile.py` | `clips/compilations/<name>.mp4` | cuts + concatenates a named list of non-contiguous VOD ranges into one landscape supercut; opt-in, never part of `run`. YouTube segments are fetched straight from YouTube (`ytsegments.py`) |
| 5d | edit | `stages/edit.py` | `work/_edits/<name>/render/<name>.mp4` | the edit engine: a spec of clips from any mix of sources -> retimed, framed, graded, decorated, scored, mastered. Not tied to one workspace; opt-in |
| 6a | upload | `stages/upload.py` | a YouTube video (id in `state.json`) | **Kick-only.** From the downloaded file (resumable; session saved in state.json) or relayed straight from Kick with no disk. Opt-in, never part of `run`; default privacy `private` |
| 6 | cleanup | `stages/download.py:delete_vod` | deletes `video.<ext>` | refuses if any approved clip/reel/compilation isn't rendered yet, unless `--force` |

### download.py

- `acquire(url, ws, settings, force=False, quality=None,
  progress=NULL_PROGRESS) -> Path` is what `cmd_download`, `cmd_run` and the
  dashboard's `download`/`pipeline` jobs actually call: a YouTube workspace
  (`ws.platform == "youtube"`) goes to `stages/youtube.py:fetch_audio`,
  everything else to `download_vod`. It returns the file
  `audio.extract_audio(video=...)` should read (`quality` means nothing for
  YouTube - no video is fetched). The one place the platforms branch, so a
  third one is one more branch here rather than four scattered ones.
- `download_vod(url, ws, settings, force=False, quality=None,
  progress=NULL_PROGRESS) -> Path`. Skips if `ws.video_path()` already
  resolves to a file.
- yt-dlp invocation: `--no-playlist --newline --write-info-json -f
  <resolved format> --progress-template <template> [--http-chunk-size
  <size>] -o <root>/video.%(ext)s --impersonate chrome <url>`, run via
  `ytdlp.run_yt_dlp` (lifted out of this module so `stages/youtube.py`
  shares it) — a `Popen`-based runner mirroring `ffrun.run_ffmpeg`'s
  shape (not `run_command`, which has neither progress nor cancellation).
- **Real progress + cancellation**, previously missing entirely for this
  stage (every other stage already had it). `run_yt_dlp` drives yt-dlp with
  `--progress-template "download:CLIPBOT_PROGRESS
  %(progress.downloaded_bytes)s|%(progress.total_bytes)s|
  %(progress.total_bytes_estimate)s|%(progress.speed)s"` — a private marker
  prefix so `ytdlp.parse_progress_line` can pick out machine-readable lines by a
  plain `startswith` check rather than regexing yt-dlp's human-readable bar
  (same reasoning `ffrun.py` gives for keying off ffmpeg's `-progress
  pipe:1` output instead of its normal log). Each parsed line calls
  `progress.update(downloaded, total=total_or_estimate, rate=speed)` —
  `Progress.update()`'s `rate` kwarg (added for this) rides along in the
  emitted payload, unlike `eta_seconds`, which `Progress` still derives
  itself from elapsed time. `-f bv*+ba/b/best`-style formats download video
  then audio as two separate files; yt-dlp's own `downloaded_bytes` resets
  when the second one starts, detected by `downloaded_bytes` dropping below
  the previous reading and triggering a fresh `progress.phase("download",
  unit="bytes")` — mirrors yt-dlp's own two-pass terminal output (video bar,
  then audio bar), not a bug. Checks `progress.cancelled` per line and
  `terminate()`/`wait(timeout=3)`/`kill()`s the subprocess on cancel, same
  pattern `run_ffmpeg` uses — a running download job's dashboard Cancel
  button previously did nothing. Every raw line is also `log.debug`-logged
  (visible under `-v`, quiet by default) — matches every other long stage in
  this codebase (transcribe/analyze/reel/compile all stay console-quiet
  during the work itself and rely on a summary log line at the end), and
  reproduces what `run_command(tee=True)`'s per-line debug logging did
  before this. Non-progress-template lines are kept as a tail (last 80),
  written to `logs/download.log` and included in the `StageError` on
  failure — same failure-reporting contract `run_command`/`run_ffmpeg`
  already provide, so the kick-dl-fallback/404-hint logic below is
  unaffected.
- **`download.http_chunk_size`** (default `"10M"`, e.g. `--http-chunk-size
  10M`; `null` omits the flag): yt-dlp's own documented flag for "bypassing
  bandwidth throttling imposed by a webserver". Added after measuring a real
  download capped at ~100Mbps over a 300Mbps line despite ~8ms latency to
  Kick's Cloudflare edge (ruling out a TCP-window/BDP explanation) —
  pointing at a per-connection throttle. **Sequential chunking only** in the
  installed yt-dlp version (checked `yt_dlp/downloader/http.py` directly —
  no threading, and `-N`/`--concurrent-fragments` is never read by the
  progressive-HTTP downloader at all, only DASH/HLS fragments), so this is a
  free, zero-dependency thing to try, not a guaranteed fix. Genuine parallel
  connections would need yt-dlp's `aria2c` external-downloader integration —
  deliberately not set up (not installed on the reference machine); a
  documented future follow-up, not built.
- **Selectable quality, threaded through every download pathway.**
  `resolve_format(settings, quality=None) -> str` turns a `quality` key
  (`"best"/"1080"/"720"/"480"/"360"`, `QUALITY_CHOICES`) into a yt-dlp `-f`
  selector via `QUALITY_PRESETS` — same `bv*[height<=H]+ba/b[height<=H]/best`
  shape as the hand-authored `download.format` default (that combo, not the
  height cap itself, is what fixed the run that silently landed on 160p);
  `"best"` means no height cap, not yt-dlp's bare `best`. `quality=None`
  (the default everywhere it isn't explicitly threaded) falls back to
  `download.format` unchanged. Only the yt-dlp path honors it — `kick-dl`'s
  fallback has no scriptable quality flag. Reachable from: `clipbot
  download --quality`/`clipbot run --quality` (CLI); the dashboard's
  per-workspace download-stage quality `<select>`
  (`server/templates/workspace.html`, id `download-quality`, also read by
  the "Run all remaining" pipeline button and forced to `"720"` by the
  low-res banner's "Re-download at 720p" action) and the Kick tab's
  quality picker (`server/templates/library.html`, shared by the "paste a
  URL" form and every channel-browser "Download" button; the YouTube tab and
  a YouTube workspace's download stage have none, since they fetch audio only)
  — both pickers are
  rendered by the one shared `qualityPickerHtml()` helper in
  `server/static/app.js`, whose option list is hand-mirrored from
  `QUALITY_CHOICES` (no route exposes that list). Dashboard job options key
  is `quality`, read by `_h_download`/`_h_pipeline` and by
  `api_create_workspace` (which forwards `payload.get("quality")` into the
  `pipeline` job it submits).
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
- `list_channel_vods(channel, limit=20)` hits
  `kick.com/api/v2/channels/{channel}/videos` and returns full metadata dicts
  (`uuid`, `title`, `url`, `created_at`, `duration_seconds`, `thumbnail`,
  `is_live`, `viewer_count`) — note the API's own `duration` field is
  **milliseconds**, converted here. Two callers: `_explain_404` (turns a 404
  into a helpful list of real VODs — Kick's live-stream session UUIDs look
  identical to VOD UUIDs but 404 if copied mid-stream — called with
  `limit=5`) and the dashboard's `GET /api/kick/{channel}/vods` (the
  "browse channel" VOD picker on the library page, so a stream can be
  downloaded by clicking instead of pasting its URL).

### youtube.py (stage 1 for a YouTube workspace)

- A YouTube workspace never needs the video on disk to find clips:
  transcription and analysis only read `audio.wav`, and the dashboard plays
  the embedded player. So this stage fetches **audio only** (~0.27 GB for a
  4.6 h stream, against ~15 GB of 1080p) and the existing
  `audio.extract_audio(video=<that file>)` turns it into `audio.wav`, which
  then deletes the source (`drop_source_audio` - only ever the workspace's own
  `source_audio.*`, never a file a caller passed in;
  `download.youtube.keep_source_audio` keeps it).
- `fetch_audio(url, ws, settings, force=False, progress=NULL_PROGRESS) ->
  Path`. Skips if `source_audio.*` exists. If `audio.wav` already exists (its
  source since deleted) it returns `audio.wav` itself - safe **only because
  `force` bypasses that early return**, so a forced re-extract never reads
  `audio.wav` as its own input (tested). Order: `fetch_metadata` → refuse a
  stream that is live or scheduled (`LIVE_NOW` = `is_live`/`is_upcoming`;
  `post_live` only warns that the archive may be incomplete) → `yt-dlp -f
  <download.youtube.audio_format> -o source_audio.%(ext)s`, then
  `mark_stage("download", audio_only=True, source_audio=..., bytes=...)`.
- `fetch_metadata` runs `--skip-download --write-info-json`, which writes
  `video.info.json` - the name Kick's download also writes, so the dashboard's
  title fallback works for both - and records `title`, `uploader` (channel
  beats uploader), `upload_date`, `stream_started_at` (release_timestamp beats
  timestamp), `youtube_duration`, `youtube_channel_id`, `youtube_live_status`.
  `youtube_duration` only *seeds* `duration` when it is absent; the audio
  probe stays the owner (the same rule as Kick's `kick_duration`). A cached
  info.json is reused only if it doesn't describe a live/post-live stream, or
  a stale `is_live` would block the audio fetch forever.
- yt-dlp flags: **none of Kick's** - `--impersonate` and `--http-chunk-size`
  are Cloudflare/throttle workarounds that don't apply, and a test pins their
  absence even when `download.impersonate` is configured. Only when
  configured: `--js-runtimes`, `--cookies-from-browser`, `--extractor-args
  youtube:player_client=` (`_common_flags`). Progress and Cancel come from the
  shared `ytdlp.run_yt_dlp`.
- **Environment (measured 2026-09-26)**: the yt-dlp on this machine's PATH
  was 2025.10.14 under Python 3.9, and it fails at format selection ("The page
  needs to be reloaded", plus a "YouTube is forcing SABR streaming" warning)
  while metadata and channel listing still work. Current yt-dlp (2026.08.19)
  needs Python 3.10+ and an external JavaScript runtime - Deno 2.3+ by default,
  or Node 22+ via `download.youtube.js_runtime` (this machine has Node 20.19
  and no Deno). ClipBot only shells out to the yt-dlp binary, so the fix is a
  separate install pointed at via `CLIPBOT_YT_DLP`, not a project Python
  upgrade. `explain_failure(text)` turns the recognisable failures (bot check,
  missing formats / JS runtime, private or members-only, unavailable) into what
  to do, and `_run` appends it to the `StageError`.
- `list_channel_streams(handle, settings, limit=20, timeout=120)`: `yt-dlp
  --flat-playlist -J https://www.youtube.com/@<handle>/streams` (no API key,
  no quota; handle validated against `^[A-Za-z0-9._-]{1,60}$`).
  **Measured**: the flat listing returns only id/title/url/thumbnails -
  `duration`, `live_status` and `view_count` come back null - so the channel
  browser shows a dash for length and cannot badge a live stream; a live
  stream is refused at fetch time instead. Thumbnails are built from
  `i.ytimg.com/vi/<id>/mqdefault.jpg` rather than taken from the listing.
- Doctor: `health_checks(settings)` is offline (yt-dlp version + age, stale
  past `STALE_YT_DLP_DAYS = 90`; JS runtime version against `MIN_DENO` /
  `MIN_NODE`), and `probe_youtube(settings)` runs `yt-dlp -F` on
  `download.youtube.probe_url` - the one check that proves extraction works end
  to end, so it runs on request only (`POST /api/doctor/youtube`).

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
  The source is, in order: an explicit `video`, `ws.source_audio_path()` (a
  YouTube workspace's audio-only download), then `ws.video_path()`; a Kick
  workspace has no `source_audio.*`, so for it this resolves exactly as it
  always did. After a successful extraction it calls
  `youtube.drop_source_audio`, so the audio download is deleted once
  `audio.wav` exists (never a caller-supplied file). A YouTube workspace with
  neither source raises a "run Fetch audio" `StageError` rather than the Kick
  cleanup-stage message.
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
  - **A third gotcha, one layer deeper still: PyTorch 2.6 changed
    `torch.load`'s default to `weights_only=True`, and pyannote's own
    checkpoints fail under it.** Measured: they store plain Python objects
    alongside tensors (`torch.torch_version.TorchVersion`, recording which
    torch version wrote the file), which the safe-unpickler used by
    `weights_only=True` doesn't recognize, so loading raises
    `UnpicklingError`. `_trust_pyannote_checkpoints()` is a context manager
    that wraps `torch.load` to force `weights_only=False` regardless of
    what the caller passes - a `functools.partial` preset default alone
    doesn't work here, because pyannote's own `pl_load` helper always
    explicitly re-passes `weights_only=weights_only` (defaulting to `None`
    at its call site), which would just override a partial's preset value
    right back. Scoped tightly around the `Pipeline.from_pretrained` call
    only, not a global setting - this is torch's own documented option (1)
    for a trusted source, and the only checkpoints loaded here are the
    official ones this stage just downloaded from Hugging Face's pyannote
    org.
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
  out_path=None, mark_stage=True, progress=NULL_PROGRESS, backend=None)
  -> Path`. **Now a dispatcher first**: `backend` (per-call override) or
  `transcribe.backend` (setting, default **`"openai"`**) picks between this
  module's local faster-whisper path (below) and
  `stages/transcribe_openai.py`'s hosted-API path — same
  override-vs-settings-default relationship `force` already has relative to
  a caller's own default. `cli.py` never passes `backend`, so the CLI
  always follows the setting; the dashboard's transcribe stage button does
  (see the server section's job-handler note).
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
  matters is the `transcript.json` shape, nothing else. `transcribe_openai.py`
  (below) is that swap, now the default.

### transcribe_openai.py (stage 3, alternate backend, now the default)

- `transcribe_audio_openai(ws, settings, force=False, audio_path=None,
  out_path=None, mark_stage=True, progress=NULL_PROGRESS) -> Path` — called
  by `transcribe.transcribe_audio`'s dispatcher, never directly.
- **Deliberately `whisper-1`, not `gpt-4o-transcribe`/`gpt-4o-mini-transcribe`**:
  only `whisper-1`'s `verbose_json` response carries per-segment
  `avg_logprob`/`no_speech_prob`/`compression_ratio` — the exact fields
  `transcribe.py`'s low-confidence flagging already depends on. The newer
  models only support `response_format=json` with per-token logprobs, a
  different shape the existing thresholds can't be applied to. $0.006/min
  — roughly $0.36/hour of audio, ~$1.66 for a 4.6h VOD (measured against
  OpenAI's published pricing, August 2026).
- **Chunking, not a single upload**: the API caps uploads at 25MB and a
  multi-hour 16kHz mono PCM `audio.wav` is hundreds of MB. `_chunk_audio`
  transcodes to 64kbps mono mp3 and splits with ffmpeg's `-f segment
  -segment_time <transcribe.openai.chunk_seconds, default 1200s/20min>`
  into `<workspace>/_transcribe_openai/` (underscore-prefixed scratch dir,
  deleted on success, kept on failure for debugging) — a 20-minute chunk at
  64kbps is ~9.6MB, comfortable margin under the cap. **Known limitation,
  accepted on purpose**: chunk boundaries are fixed-time cuts, not
  silence-aware, so a word can occasionally split across a chunk edge — not
  engineered around, because the existing low-confidence flagging already
  exists to catch exactly this kind of artifact (a boundary-mangled segment
  scores a low `avg_logprob` and gets flagged, never silently trusted).
- **Chunks upload with bounded thread-pool parallelism**
  (`transcribe.openai.concurrency`, default 4) — this, not any per-request
  speed difference, is the actual "faster wall-clock" mechanism versus local
  mode's single-threaded ~0.8x realtime. Same "network I/O, not
  CPU-bound" justification `server/jobs.py` already gives for using threads
  over processes.
- **Per-chunk retry** (`transcribe.openai.max_retries`, default 3, with
  backoff); a chunk that still fails after retries gets one synthetic
  segment spanning its time range (`text=""`, `low_confidence=true`,
  `flags=["openai_chunk_failed"]`) rather than aborting the whole run — same
  count-failures-don't-abort precedent `transliterate.py`'s
  `failed_batches` already sets. Tracked as `failed_chunks` in the output.
- Segment `start`/`end` are offset by `chunk_index * chunk_seconds` and
  renumbered sequentially across the whole file, reusing
  `transcribe.py`'s existing `low_confidence_threshold`/
  `max_segment_seconds` thresholds against the returned per-segment stats
  — same flagging logic, same schema, different segment source.
- `transcript.json`: same shape `transcribe.py` documents, plus
  `"backend": "openai"` and `"failed_chunks": <int>`. `device`/
  `compute_type` are harmless placeholders (`"openai-api"`/`"n/a"`) since
  nothing downstream branches on them.
- Logs an estimated cost (duration from `state.duration`, never
  `kick_duration` — the usual invariant — × `$0.006/min`) before spending
  any money, in both CLI and dashboard log streams.
- Needs `pip install -U openai` (in `requirements.txt` directly — small
  pure-Python SDK, unlike `requirements-diarize.txt`'s multi-GB PyTorch) and
  `OPENAI_API_KEY` in the environment (`transcribe.openai.api_key_env`
  names the var, default `OPENAI_API_KEY`) — checked unconditionally by the
  dashboard's `/api/doctor`, same treatment `ANTHROPIC_API_KEY` gets, since
  this is now the default transcribe path, not a conditional opt-in.

### analyze.py

- `analyze_transcript(ws, settings, force=False, progress=NULL_PROGRESS) -> Path`.
- **Long transcripts are analyzed in overlapping windows, not one call.**
  Measured on a real 5.1h/4148-segment stream: a single call found clips
  densely for the first ~3 hours, then went **83 minutes without picking
  anything**, despite stopping voluntarily (`stop_reason="end_turn"`, under
  6% of its 32000-token output budget used) — a long-context "lost in the
  middle" recall problem, not a token-limit problem. `analyze_transcript`
  builds windows via `_build_windows(duration, chunk_minutes,
  overlap_minutes)` only when `duration > analyze.chunk_threshold_minutes *
  60` (default 90 min) — shorter streams take the exact same single-call
  path as before, byte-identical prompt (see `build_prompt`'s trailing
  per-line `.rstrip()`, added specifically so a blank `{{WINDOW_NOTE}}`
  doesn't perturb the cached prompt on the common, non-chunked path).
  Each window covers a "core" range (`analyze.chunk_minutes`, default 60)
  plus `analyze.chunk_overlap_minutes` (default 8) of **context-only**
  padding on each side; `_window_note()` builds the `{{WINDOW_NOTE}}` text
  that instructs the model to only propose clips whose `start_time` falls
  inside its own core range — this headers off most cross-window duplicates
  *before* they're generated, rather than relying only on `validate_clips`'s
  post-hoc overlap trimming. Windows run through `_call_model` (the shared
  single-call implementation, used by both the chunked and non-chunked
  paths) via a `ThreadPoolExecutor(max_workers=analyze.concurrency)`
  (default 2) — same network-I/O-in-threads reasoning `server/jobs.py`
  already documents for the dashboard's job lanes. A window that errors
  doesn't abort the run (same count-failures-don't-abort precedent as
  `transliterate.py`'s `failed_batches`); `usage` in the output is summed
  across every chunk call, `stop_reason` is `"max_tokens"` if any chunk hit
  it else `"end_turn"`.
- **Notable-moment signals** (`clipbot/highlights.py`, below) annotate
  transcript lines with `(energy spike)` / `(chat spike, Nx)` tags derived
  from audio loudness and chat message rate, independent of the transcribed
  words — applies on every run, chunked or not, gated by
  `analyze.signals.enabled`.
- `build_prompt(transcript, settings, state, segments=None, window_note="",
  signals=None)`: loads `analyze.rubric_file` (`config/rubric.md`) and
  `analyze.prompt_template` (`config/analysis_prompt.md`), strips `<!-- -->`
  comments from both, substitutes `{{TRANSCRIPT}}` (via `format_transcript`,
  which now also takes `signals` for the spike annotations) / `{{RUBRIC}}` /
  `{{DURATION}}` (always the *full* stream length, even inside a window -
  the window's own range is conveyed by `{{WINDOW_NOTE}}` instead) /
  `{{STREAM_TITLE}}` / `{{WINDOW_NOTE}}`. `segments` overrides
  `transcript["segments"]` for a chunked window's padded slice.
- **Prompt caching**: splits the template on the literal marker
  `{{CACHE_BREAKPOINT}}` — everything above (the transcript) is sent with
  `cache_control: {"type": "ephemeral"}`; everything below (the rubric +
  task instructions) is not. This ordering is deliberate: editing only the
  rubric and re-running rebills roughly 10% of input cost within the cache
  TTL. **If you reorder `analysis_prompt.md`, you lose this savings.** On
  the chunked path each window is its own cacheable block, so a rubric-only
  re-run still gets cheap reads per window; the ~8min overlap between
  adjacent windows is sent (and cached) twice, a modest, expected overhead.
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
  chunked, chunk_count, signal_count, stop_reason, usage: {input_tokens,
  output_tokens, cache_creation_input_tokens, cache_read_input_tokens},
  clips: [{start_time, end_time, description, why}]}`. `rubric_sha1` is what
  lets you tell which version of your criteria produced a given set of
  picks; `chunked`/`chunk_count` record whether this run went through the
  windowed path; `signal_count` is how many energy/chat-spike spans
  `highlights.notable_moments` found for this run.
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

### highlights.py

- Not a pipeline stage — a helper `analyze.py` calls, added specifically
  because the transcript it sends Claude is 100% text, so a moment that's
  pure laughter or a loud reaction with no distinctive words is invisible
  to the model no matter how the rubric is worded.
- `compute_energy_spikes(audio_path, bin_seconds=5.0, z_threshold=2.0) ->
  [{start, end, z_score}]` — reuses `chatsync.audio_energy_envelope`
  (already streams `audio.wav` in O(1) memory, no new way of reading audio)
  and z-scores each bin against the whole-VOD mean/std, merging adjacent
  outlier bins into spans.
- `compute_chat_spikes(chat_doc, offset_seconds, duration, bin_seconds=15.0,
  z_threshold=2.0) -> [{start, end, z_score, message_rate_multiplier}]` —
  reuses `stages.chat.messages_between` (the existing shared query
  function, already handling the chat-clock-vs-VOD-clock offset
  correction) to bin message counts, same z-score/merge logic.
- Both are z-score outlier detection against that signal's own whole-VOD
  mean/std, not a fixed threshold — a stream's own baseline loudness/chat
  activity varies too much (quiet talking vs. an intense game moment) for
  one absolute cutoff to mean the same thing throughout.
- `notable_moments(ws, settings) -> [{start, end, label}]` is the entry
  point `analyze.py` calls — merges both signals, sorted by start.
  Degrades gracefully by design: skips (logs, doesn't raise) the energy
  signal if `audio.wav` is missing, skips the chat signal if `chat.json` is
  missing or its `status` isn't `"ok"`, and returns `[]` outright if
  `analyze.signals.enabled` is `false`. A notable-moments list is a hint,
  not a hard dependency — a broken signal must never sink the analyze
  stage.
- `analyze.format_transcript`'s `signals` argument annotates any transcript
  line whose time range overlaps a moment span with its `label` (e.g.
  `(energy spike)`, `(chat spike, 3.1x)`), appended the same way
  `(low-confidence)` already is. Applies on every analyze run regardless of
  whether the chunked windowing path is engaged.

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
  below.) For a YouTube cut a `source_tag` (`StreamResolver.tag()`, i.e.
  `yt:<video id>:<format ids>`) is appended as a fifth field **only when
  given**, so a Kick clip's fingerprint stays byte-identical (pinned in
  `test_youtube_segments.py`) and a YouTube clip is re-cut when the best
  format changes (HD finishing processing).
- **YouTube workspaces** (no video on disk, `ws.source_mode() == MODE_EMBED`):
  instead of raising the "No video" error, `cut_clips` builds a
  `ytsegments.StreamResolver` and fetches each clip's padded range with
  `ytsegments.fetch_segment` - only those seconds cross the network. **Always
  re-encoded**, whatever `cut.re_encode` says (a stream copy from two separate
  remote inputs has no clean start, see the ytsegments.py section), with
  `cut.encoder`/`cut.preset`/`cut.crf`; `output.re_encode` is recorded `true`.
  Per-clip ffmpeg log: `logs/cut-<clip id>.log`. A Kick workspace whose video
  was deleted still gets the original error. Reached from `clipbot cut`,
  `clipbot run --cut-all`, and the dashboard's `cut` job (review page "Cut
  approved", workspace page "Fetch approved clips").
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
  `/reel/plan` and don't actually render in the `/reel/preview` proxy. (An
  unused `reel.preview.include_chat` setting used to imply otherwise; it's
  been removed rather than left as dead config, same call made on the
  `benchmark` job kind below.) The dashboard's live canvas (`reel.js`'s
  `drawReelPreview`) compensates by drawing chat/captions/speakers as a
  labelled reserved-space rectangle instead of a facsimile of the real
  content. Speakers needed one more piece to get this treatment at all:
  unlike chat/captions, a speaker slot's position depends on which speakers
  actually talk in a given clip (`speaking_spans()`, workspace IO), which
  `reelspec.resolve()`'s pure math alone has no way to compute. Resolved by
  splitting `speakerfx.resolve_speakers()`'s render-plan pipeline: the pure
  "rebase spans onto this clip, find who's active, lay out slots" prefix is
  now its own function, `speakerfx.resolve_speaker_slots()` — no avatar/ring
  image generation, so it's safe for `api_reel_plan` to call on every
  debounced spec edit, unlike the full `resolve_speakers()` the real render
  uses. `api_reel_plan` calls it and attaches the result as
  `plan["speaker_slots"]` (each slot's rect plus the speaker's name/color,
  looked up from `library.list_speakers`), which `drawReelPreview` now draws
  the same reserved-rectangle way as chat/captions.
- `unrendered_reels(ws)` mirrors `uncut_approved` — blocks VOD deletion
  while reel-configured clips lack a rendered file (reels re-encode from
  source, so losing the VOD strands them). `stages/compile.py`'s
  `unrendered_compilations(ws)` is the third sibling in this family, checked
  by both the CLI's `cmd_cleanup` and the dashboard's `_h_cleanup` alongside
  `uncut_approved`/`unrendered_reels` — previously only the dashboard
  checked all three; the CLI checked none but `uncut_approved`.

### compile.py (stage 5c)

- A third sibling of cut/reel, for the same "different output, different
  idempotence key" reason reel is separate from cut: cut produces N
  archival per-clip files, reel produces one vertical re-encode per clip,
  compile produces **one landscape video assembled from several
  non-contiguous source ranges** — a supercut, e.g. a curated YouTube
  highlights reel. `clip["output"]`/`clip["reel_output"]` live on
  `clips.json`; a compilation's state lives entirely in its own sidecar,
  `compilations.json` (owned by `clipbot/compilations.py`), **not**
  `clips.json` — a compilation's segments must never be picked up by a
  normal `clipbot cut` run as individual clip outputs, so they're kept out
  of the approve/reject/cut review queue entirely rather than added via
  `review.add_manual_clip`.
- `render_compilation(ws, settings, name, force=False, progress=NULL_PROGRESS)
  -> Path`. Looks up the named compilation's `segments` (`{start, end,
  label, slug}`) from `compilations.json`.
- **Cross-stream**: each segment carries a `slug` naming which workspace its
  footage comes from — defaults to the home workspace (`ws`, the one this
  compilation's `compilations.json`/scratch dir/output live in) when
  omitted, so an all-local compilation is unaffected. `_resolve_source`
  opens (and caches per render) whichever workspace a segment names — always
  `ws` itself for the home slug, never re-derived from `settings.work_root`,
  since the CLI's `--workspace` accepts an arbitrary path, not just a slug
  under `work_root`. A segment naming a workspace with no video (wrong slug,
  or deleted by cleanup) raises a `StageError` naming the segment index and
  slug. Padding (`cut_stage._padded_range`) clamps against *that source's
  own* `state.duration`, not the home workspace's. `_segment_fingerprint`/
  `_compilation_fingerprint` both hash the resolved `slug` alongside the
  numeric range, so two segments from different sources with a coincidentally
  identical range can't collide in the scratch cache.
- **Ordering/overlap is source-aware** (`compilations._normalize_segments`):
  when every segment in a compilation shares one `slug`, segments are sorted
  by `start` and checked for overlap across the whole list, exactly as
  before `slug` existed. Once a compilation spans multiple sources, the
  *given* order is kept as the play order instead — one source's timestamp
  has no relationship to another's, so "chronological" would be meaningless;
  a montage spanning streams is editorially sequenced, not time-sorted — and
  overlap is only checked *within* each source's own segments. `compile.js`'s
  client-side segment list applies the identical branch (`isSingleSource()`)
  before its own local re-sort, so adding a same-stream segment to an
  already-mixed compilation via the page's timeline editor doesn't scramble
  the cross-stream ordering back into raw numeric order.
- Padding reuses `cut_stage._padded_range` as-is — same `cut.pad_start`/
  `cut.pad_end` settings reel.py already reuses rather than inventing its
  own padding knob.
- **Every segment is always re-encoded, never stream-copied** (`compile.encoder`/
  `compile.preset`/`compile.crf`, deliberately not inheriting `cut.encoder`/
  `cut.preset`/`cut.crf`, same "doesn't inherit" precedent as
  `reel.preset_x264` not inheriting `cut.preset`): the final join uses the
  concat demuxer's `-c copy` mode, which requires every input to share
  identical codec parameters, and a stream-copy cut snaps to the nearest
  keyframe (fine for an archival clip, not for a boundary that has to land
  exactly where a highlight was picked).
- Each re-encoded segment is cached in a per-compilation scratch directory
  (`ws.compile_scratch_dir(name)` = `root/_compile/<name>/`, filename keyed
  by a hash of its resolved range + encode settings) and **not** deleted
  after a successful render — a re-render skips any segment whose resolved
  range and settings are unchanged, the same skip-if-unchanged discipline
  cut.py applies per clip.
- The join step writes an ffconcat list of bare segment filenames (all in
  the same scratch directory, so no path-escaping is needed) and runs
  `ffmpeg -f concat -safe 0 -i list.ffconcat -c copy -movflags +faststart`
  — a fast, lossless join of the already-uniformly-encoded segments.
- Whole-compilation idempotence key is a hash of the compilation name +
  every resolved segment range + the encode/padding settings
  (`_compilation_fingerprint`) — self-maintaining the same way reel.py's
  argv hash is, rather than a hand-listed field tuple like cut.py's.
- **YouTube segments** (a source workspace with no video on disk,
  `source_mode() == MODE_EMBED`): `render_compilation` resolves each segment
  to either a local video `Path` (the code path and fingerprints above,
  unchanged) or a per-workspace `ytsegments.StreamResolver` (one per YouTube
  workspace named, created lazily), and fetches the remote ones with
  `ytsegments.fetch_segment` into the **same scratch directory** with the same
  `compile.encoder`/`preset`/`crf` - so the concat join, the skip-unchanged
  cache and the output record are shared with the local path. Re-render
  behaviour matches local: an unchanged compilation fetches nothing, adding a
  segment fetches only the new one. Both fingerprints take a YouTube tag
  (`_segment_fingerprint(..., source_tag)` / `_compilation_fingerprint(...,
  source_tags)`, appended **only when non-empty** - the golden values
  `c6d4e66d6c86` / `451ae5be992115f2` in `test_youtube_segments.py` pin that
  local ones did not move), so a better resolution of the same video (HD
  finishing processing) redoes the affected segments.
  `_check_youtube_sources(resolved, resolvers)` runs before anything is
  fetched and refuses **(a)** a compilation mixing YouTube and local segments
  and **(b)** YouTube segments whose videos differ in width/height/fps - the
  join is a stream copy of independently encoded pieces and this stage has no
  scale/fps normalisation. Several YouTube videos with identical shape are
  allowed. Per-segment ffmpeg logs, as for local segments:
  `logs/compile-<name>-<NNN>.log`.
- Output recorded via `compilations.set_output`: `{file, bytes, duration
  (sum of padded segment spans — exact, since the join is a lossless
  stream copy), rendered_at, fingerprint}`, at
  `clips/compilations/<name>.mp4` (a subdirectory of `clips_dir`, same
  treatment `reels_dir` gets, so compilations don't inflate the flat
  per-clip listing).
- `unrendered_compilations(ws)` mirrors `cut.uncut_approved`/
  `reel.unrendered_reels` — blocks VOD deletion while a compilation *homed
  in `ws`* has segments but no rendered file (every segment re-encodes from
  the source video, so losing it strands them the same way an un-rendered
  reel does). `unrendered_compilations_elsewhere(ws, settings)` is the
  cross-stream counterpart: since a compilation homed in a *different*
  workspace can still have a segment sourced from `ws`, it scans every other
  workspace under `settings.work_root` (same "iterate work_root's dirs"
  pattern `server/app.py`'s `api_workspaces()` uses) for an unrendered
  compilation with a segment whose `slug` resolves to `ws`, returning
  `{"workspace", "compilation"}` pairs naming where the dependency lives.
  Both checks are run by both the CLI's `cmd_cleanup` and the dashboard's
  `_h_cleanup`.
- CLI: `clipbot compile --workspace <slug> --name <name> --range
  [slug:]start,end[,label] [--range ...] [--force]`. `--range` upserts
  (replaces, not merges) the named compilation's segment list before
  rendering; omitting it entirely just re-renders the existing definition.
  An optional `slug:` prefix on a `--range` (detected via
  `RANGE_SLUG_RE` — unambiguous since a range's `start` is always numeric
  and a slug never is) sources that one segment from a different workspace
  than `--workspace`, for a compilation spanning multiple streams. This is
  the mechanism a Claude Code session drives directly (after reading
  `candidates.json`/`transcript.json` across workspaces and talking through
  which moments to use with the user) rather than any new Claude-API-driven
  picker stage — cross-stream compilation authorship is deliberately a
  conversation-plus-CLI workflow, not automated.
- Also reachable from the dashboard — `/w/{slug}/compile`
  (`templates/compile.html` + `static/compile.js`), see the dashboard
  section below. The CLI and the dashboard write the exact same
  `compilations.json` sidecar, so a compilation created by one is fully
  visible and editable in the other (verified: `clipbot compile` output
  shows up in the dashboard's list with its rendered file playable, and
  `external_activity()` surfaces a CLI-driven compile run as busy instead
  of idle). The dashboard's role for a cross-stream compilation is
  **review and render only, not authoring**: the page's timeline/transcript
  editor is bound to one workspace's own video, so a segment whose `slug`
  differs from the page's own gets a "from `<slug>`" badge in the segment
  list and its ✎ edit button disabled (tooltip points at the CLI) — ✕
  remove, Save, and Render all still work on the full mixed list.

### killfeed.py (stage 2c)

- `scan_killfeed(ws, settings, player=None, video=None, force=False,
  keep_video=False, progress=NULL_PROGRESS) -> Path` writes `killfeed.json` and
  `killfeed_icons/kNNN.png` into the workspace; `load(ws)` reads it back.
  Settings: `killfeed.player/fps/scale/crop/chunk_seconds` (`crop` is `W:H:X:Y`
  in a 1280x720 frame; the default `620:230:0:440` is Wardogs' feed).
- Method: decode every frame at `killfeed.fps` (1/s), crop the feed, upscale
  3x, OCR with the built-in **Windows OCR** (`data/windows_ocr.ps1` via
  PowerShell + WinRT - no install, nothing leaves the machine; isolated in
  `_ocr_dir` so another backend is one function), then `killfeed.observe()` ->
  `group_runs()` -> events. A YouTube workspace has no video on disk, so the
  picture track is copied to scratch (`ytsegments.download_video_track`) and
  deleted afterwards unless `keep_video`. Dense decode, not keyframe seeking:
  YouTube keyframes are ~6 s apart and a feed row can come and go between two.
- **The feed row is `[killer][weapon icon][skull][N m] victim`.** Only rows that
  involve the local player carry the distance tag. The player's name in the
  *killer* position (x <= `LEFT_MARGIN`=260 at 3x) is a kill; the name after the
  tag is a death and is skipped. A distance is read as the **mode** across the
  frames a row is on screen (OCR misreads the `[` as a leading `1`, so
  `drop_prefixed_twins` removes `110` sitting beside a better-supported `10`).
  Name matching (`make_matcher`) accepts one edit (a dissolved first/last letter
  included); names under 4 letters match exactly.
- `killfeed.json`: `{schema, player, video, fps, crop, scale, duration, frames,
  scanned_at, kills, nades, headshots, events[]}`; an event is `{id, t, first,
  last, dist_m, n_obs, victim_raw, victim, weapon (gun|grenade|other|unknown),
  headshot, through_cover, strength, blobs, icon}`. `t` is video seconds, half a
  frame before the row first appeared. The icon crop stops at the last modifier
  icon (`icon_extent`) so the distance digits never leak into a callout.
- **Limits, by design:** a stream whose webcam/overlay covers the feed (several of the
  user's Sep/Oct 2026 Wardogs streams) yields nothing - reported as "no
  kills", which means "unreadable" there. Victim names in other scripts come back
  empty (`readable_name`). Calibrated on 1280x720 Wardogs footage only.

### edit.py (stage 5d)

See "The edit engine" for the model; this is the IO. `render_edit(spec,
settings, preview=False, force=False, progress=NULL_PROGRESS, master=True) ->
report` where `spec` is a path or a dict and `report` carries `output`,
`duration`, `frames`, `clips`, `xfade`, `measured_input`, `pacing`, `warnings`,
`render_seconds`, `cache`. Phases: prepare (open + probe every source) ->
segments -> clips -> assemble -> mix -> master -> mux.

- **Segments**: each clip's `[in - pad, out + pad]` (`edit.segment_pad`, 0.6 s)
  is cut once into `work/_cache/edit/seg/<sha>.mp4` - x264 veryfast crf 12,
  **GOP 15** (`SEGMENT_GOP`), ALAC audio - keyed by the source's tag, the range
  and the GOP, so edits share them. From YouTube it is
  `ytsegments.fetch_segment(..., audio_args=, video_args=)`; from a file one
  ffmpeg cut. The short GOP is what makes the per-piece `-ss` seeks cheap.
- **Clips**: one ffmpeg per clip -> `cache/<final|preview>/clips/<id>-<fp>.mp4`
  (video only) + `.wav` (PCM), named by a fingerprint of everything that shapes
  them (`_clip_fingerprint`), so changing one clip re-encodes only that clip and
  its neighbours' transition edges. Written as `.part.*` and renamed.
- **Assemble**: all joins are cuts -> `ffconcat` + `-c copy` (no second encode,
  clips encoded at *final* quality); any xfade -> a filter graph, clips encoded
  at the *intermediate* tier (veryfast crf 14) and the assembler makes the final.
  Preview tier is `ultrafast`; `preview_output()` is the same frame shape at
  shorter side 540 (never larger than the final), <=30 fps.
- **Master**: music/SFX mixed (`mix_graph`: beds sidechain-ducked by the game
  audio, `amix normalize=0`), then `measure_loudness` (loudnorm pass 1) and a
  second, **linear** loudnorm pass + `alimiter` + AAC once; the picture is
  `-c copy` muxed beside it, so an audio change never costs a video generation.
  The limiter ceiling is `true_peak_db - 0.7` (`LIMITER_MARGIN_DB`) so the AAC
  encode stays under the ceiling; a peaky mix lands up to ~1.5 LU under the
  target (the verifier passes within 1.5).
- Everything uses `-/filter_complex <file>`: **`-filter_complex_script` was
  removed in ffmpeg 8** and a 40-piece graph is far over any argv limit.
- Layout of `work/_edits/<name>/`: `spec.json` (the author's - **a render never
  overwrites it**; `edit new --force` keeps the old as `spec.previous.json`),
  `facts.json` (player, game and the kills/nades/longest distance the edit *contains* - not the input count - for `deliver`'s titles), `render/<name>.mp4`,
  `render/<name>.preview.mp4`, `render/<name>.<final|preview>.spec.json` (what that
  render used - a preview's 540p output block lives here, not in spec.json) and
  `.timeline.json`, `verify/<final|preview>/`, `deliver/`, `cache/`. Underscore-
  prefixed `_edits` so the dashboard's workspace list and slug validator ignore it.
- A source is `{"workspace": slug}` (video on disk, else a YouTube workspace) or
  `{"file": abs path}`. **`Workspace.video_path()` scans for the largest video-
  extension file, and `source_audio.webm` (YouTube's audio-only download) counts
  as one** - a YouTube workspace that still has it reads as "has video on disk" and
  breaks `compile`; `clipbot audio` removes it. Not fixed.

### upload.py (stage 6a: a Kick VOD -> YouTube)

- `upload_vod(ws, settings, force=False, privacy=None, title=None,
  description=None, mode=None, quality=None, progress=NULL_PROGRESS) -> dict`.
  Returns, and records with `mark_stage("upload", ...)`, `{video_id, url,
  studio_url, privacy, mode, title, uploaded_at[, bytes]}`; also writes
  `youtube_video_id`/`youtube_url` to state.json. **Idempotent**: a workspace
  with the `upload` stage marked returns that record with no network traffic
  unless `force` - every upload makes a new video. Refuses a YouTube
  workspace. `youtube_auth.TokenSource` is built before anything is resolved or
  downloaded, so missing setup fails first and cheaply.
- **Never part of `run`/`pipeline`**: it publishes to an external service, so
  it only runs when asked (CLI `upload`; the dashboard's "Upload to YouTube"
  row and "To YouTube" button, both of which confirm first). Default
  visibility is `private` (`upload.youtube.privacy`).
- **Modes** (`upload.youtube.mode`, `--mode`; `MODES = auto, stream, file`).
  `auto`: file if `ws.video_path()` or a complete spool exists, else stream.
  `file`: upload the file, running `download.acquire` first if there is none.
  `stream`: never touch disk.
- **File path** (`_upload_file`): the session URI is kept in
  `state.json["upload_session"]` as `{uri, size, mtime, file, started_at}`. A
  re-run `probe()`s it (`bytes */size`) and resumes at the server's offset when
  size, mtime and name still match. A dead session (404) starts a new one, once.
- **Stream path** (`_upload_stream`): `resolve_vod_stream()` runs `yt-dlp -J -f
  <download.format or --quality> --impersonate chrome` (the download's own
  selector and impersonation); `vod_stream_from_info()` picks one muxed URL, or
  video + audio; `stream_argv()` stream-copies it (`-c copy`, no re-encode) to
  **stdout** in a container writable without seeking - fragmented MP4
  (`+frag_keyframe+empty_moov+default_base_moof`, the default), or Matroska /
  MPEG-TS via `upload.youtube.stream_container`. `-progress` goes to stderr
  (`pipe:2`) because stdout carries the media. `FfmpegRelay` is the file-like
  source and `ytupload.ChunkReader` reads it ahead on a thread, so the read from
  Kick and the write to YouTube overlap. Memory is about three chunks
  (`upload.youtube.chunk_mb`).
- **ffmpeg 8 needs `-bsf:a aac_adtstoasc`** to stream-copy HLS's ADTS AAC into
  MP4/Matroska: older builds inserted it themselves, ffmpeg 8's muxer fails with
  "Malformed AAC bitstream". `stream_argv` adds it only when the input is HLS
  (`_is_hls`: a `.m3u8` URL or an `m3u8*` protocol), because the filter errors on
  AAC that is already raw, and never for MPEG-TS. Found by the real-ffmpeg test,
  not by the mocked ones.
- **A truncated VOD is never confirmed.** `FfmpegRelay.read` checks at EOF - a
  non-zero ffmpeg exit, or `media_seconds` short of `info.duration` by more than
  `max(SHORTFALL_SLACK_SECONDS=120, 3%)` (Kick's own duration runs ~30s long,
  see the download.py section) - and raises *before* the uploader sees the end
  of input. `upload_stream` reads one chunk ahead for exactly this reason: the
  final chunk is the one that declares the total and completes the upload, and
  it is not sent until the source has been verified.
- **Unknown-length fallback.** YouTube's guide documents declared sizes only. If
  `start_session(size=None)` or the first `bytes 0-N/*` chunk is refused
  (`UnknownLengthUnsupported`, carrying the unsent chunks in `.pending`), the
  stage spools `pending` plus the rest of the source to `_upload/spool.mp4`
  (written as `.part`, renamed once whole) and uploads that with a known size;
  the record's `mode` is then `stream+spool`. The spool is in a subdirectory
  because `Workspace.video_path()` scans the workspace root for video extensions
  and must never mistake it for the VOD. Removed after a successful upload.
- A stream-only workspace has no `video.info.json`; `_adopt_metadata` records the
  keys a download would (`download.metadata_from_info`, split out of
  `_read_metadata` for this) but never overwrites an existing value - `duration`
  belongs to the audio probe.
- Title/description come from `upload.youtube.title_template` /
  `description_template` with `{title} {channel} {date} {slug} {url}` (unknown
  keys render empty; a malformed template falls back to the title).
  `ytupload.clean_title` replaces `<`/`>` (YouTube rejects them) and cuts to 100
  characters; `clean_description` cuts to 5000 *bytes* on a character edge.
- **Unverified against live YouTube.** Everything above is tested against an
  in-process fake of the protocol and a real ffmpeg, not Google. Two facts only
  a real upload settles: whether YouTube accepts an upload of unknown length
  (the spool fallback exists because it may not), and whether an unverified API
  project's uploads are forced private (Google's reference now says they are
  not; its July 2020 revision note said they were).

### ytupload.py (the resumable protocol)

- Speaks HTTP through `http.client` directly - not Google's client library
  (which wants a seekable stream and re-reads it on retry, which a relayed VOD is
  not) and not `urllib` (which treats the protocol's `308 Resume Incomplete` as a
  redirect).
- Protocol: `POST ...?uploadType=resumable` -> `Location` session URI; `PUT
  session` with `Content-Range: bytes S-E/TOTAL` (or `/*`); `308` + `Range:
  bytes=0-N` means N+1 bytes are held; `200/201` is done and the body is the
  video resource; an empty `PUT` with `bytes */TOTAL` is a status query. Chunks
  are multiples of `CHUNK_UNIT` (256 KiB) except the last; `chunk_bytes` is
  rounded down to one.
- `_transmit(session, data, start, total)` is the core. A transient failure
  (network error, 408/429/5xx, a 403 rate-limit reason) backs off, asks the
  server what it holds, and carries on; a **partial** acknowledgement resends
  only the remainder; a server offset *behind* what was already sent raises (a
  live stream can't be rewound and skipping bytes would corrupt the video). The
  retry count resets whenever the committed offset advances. A `401` refreshes
  the token once. Cancellation is checked between chunks and during backoff.
- `upload_stream` holds `cur` until it is acknowledged and reads `nxt` first, so
  the last chunk (`nxt == b""`) carries `/TOTAL`; a source that is an exact
  multiple of the chunk size therefore finishes without an empty finalising PUT.
- `explain_api_error` maps `quotaExceeded`, `uploadLimitExceeded`,
  `youtubeSignupRequired`, `videoTooLong`, auth reasons etc. to what to do.

### youtube_auth.py (OAuth for uploading)

- Loopback "installed app" flow with PKCE (`pkce_pair`, S256) and a `state`
  check, stdlib only. The only scope is `youtube.upload`: it can add videos and
  cannot read or change anything else on the channel.
- ClipBot ships no credentials. The user's "Desktop app" OAuth client JSON goes at
  `upload.youtube.client_secret_file` (default `config/youtube_client_secret.json`;
  `.gitignore` also covers `client_secret*.json` and `youtube_token*.json`);
  `load_client` rejects a `web` client and prints the setup steps. The refresh
  token is stored at `upload.youtube.token_file`, else
  `<work_root>/_cache/youtube_token.json`, and is tied to the client id (a login
  from another client is not reused).
- `TokenSource` is callable (`source(force_refresh=False)`), thread-safe, and
  refreshes `EXPIRY_MARGIN_SECONDS` (120) before expiry, since a multi-hour
  upload keeps asking. `invalid_grant` is reported with the usual real cause: a
  consent screen left in **Testing** expires refresh tokens after 7 days;
  publishing it ("In production", unverified is fine for personal use) stops it.
- `status(settings)` is offline and returns `state` (`not_configured |
  needs_auth | ready`), `authorized` and a `detail` string. The Doctor and
  `POST /api/workspaces` use it; `not_configured` is deliberately not a Doctor
  failure because the feature is optional.

### ytsegments.py (cutting straight from YouTube, used by stages 5 and 5c)

- `fetch_segment(resolver, start, end, out_path, settings, encoder, preset,
  crf, progress=NULL_PROGRESS, base=0.0, span=1.0, log_path=None, label=None)
  -> None` cuts `[start, end]` (seconds on the video's own clock) out of a
  YouTube video into `out_path`, **re-encoded**. Only the requested seconds
  cross the network. Raises `StageError` on failure without leaving a partial
  file; `JobCancelled` propagates untouched (the caller unlinks, as cut and
  compile already do for local cuts). `base`/`span` place this segment's 0..1
  encode progress inside the caller's phase total, the same arguments
  `ffrun.run_ffmpeg` takes.
- **Why not `yt-dlp --download-sections`** (measured 2026-09-26, yt-dlp
  2026.08.19, ffmpeg 8.1.2): it sat at 0 % CPU for over five minutes on a
  30-second section. yt-dlp hands ffmpeg a googlevideo URL, and ffmpeg's http
  protocol opens ONE unbounded range (`Range: bytes=0-`) by default; to seek it
  "soft-seeks" by draining the rest of that response, and googlevideo throttles
  unbounded ranges to ~31 KB/s (bounded requests ran at 4-12 MB/s, even 15 MB
  into the file). `-request_size N -multiple_requests 1` on every input makes
  each request bounded: the same seeks then took 0.4 s (audio) and 1.2 s (video,
  1000 s in). yt-dlp's section download passes neither, so this module uses
  yt-dlp only to resolve the URLs and drives ffmpeg itself.
- `resolve_streams(url, settings, timeout=180) -> Streams`: one `yt-dlp -J -f
  <segment format> <url>` (through `youtube_stage.base_argv`, so the configured
  JS runtime / cookies / player client apply) returning `Streams(video_url,
  audio_url, headers, width, height, fps, expires_at, format_ids,
  video_duration, audio_duration)` - a video-only and an audio-only URL, or
  `audio_url=None` when yt-dlp picked one muxed format; the two durations are
  each track's own length from the `dur=` in its URL (12502.466 / 12502.528 on a
  real stream whose `info.duration` said 12503), `None` if a URL lacks it.
  `DEFAULT_SEGMENT_FORMAT` (override `download.youtube.
  segment_format`) is **progressive-https only** - HLS/DASH-manifest formats
  can't be range-read this way - preferring H.264 + AAC up to 1080p; a
  non-http protocol raises a `StageError` explaining that a stream that has
  only just ended is still being processed. Measured: the 2026.08.19 `visionos`
  client returns exactly such progressive https formats.
- `StreamResolver(ws, settings)`: one per cut/compile job (and per YouTube
  workspace a compile names). Reads `url`/`video_id` from `state.json`.
  `get(refresh=False)` resolves lazily, caches, and re-resolves when the URLs
  have under `REFRESH_MARGIN_SECONDS` (600) left or on `refresh=True` -
  googlevideo URLs carry `expire=` (~6 h) and are bound to the requester's IP.
  `tag()` = `yt:<video_id>:<format_ids>`, the fingerprint tag cut.py and
  compile.py append.
- `segment_argv(binary, streams, start, end, out_path, encoder, preset, crf,
  size, audio_bitrate="160k")`: per input `-request_size N -multiple_requests 1
  -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rw_timeout
  20000000 [-user_agent ..] [-headers ..] -ss <start> -i <url>` (each input
  seeked independently, `-ss` before its own `-i` so it is a range seek), `-map 0:v:0
  -map 1:a:0` (`0:a:0` for a muxed URL), `-t <span>`, encoder/preset/crf,
  `-pix_fmt yuv420p` (a 10-bit VP9/AV1 source would otherwise come out as High10
  H.264, which browsers and Instagram won't play), aac 160k, `+faststart`. All
  `-i` come before `-t`.
- `request_size(settings)`: `download.youtube.request_size` (default 1 MiB)
  **clamped to [64 KiB, 8 MiB]** - 16 MiB requests were measured throttled to a
  crawl again and 1 MiB was fastest, so a mistyped setting can't recreate the
  hang.
- `fetch_segment` retries **once**, with freshly resolved URLs, when ffmpeg's
  error looks like a rejected or expired URL (`_REJECTED_MARKERS`: 403,
  forbidden, 404 not found, 410 gone, "server returned 4"); anything else is
  not retried. `-request_size` **first shipped in ffmpeg 8.1** - absent from
  7.0, 7.1 and 8.0 (checked against `libavformat/http.c` and
  `doc/protocols.texi` at each tag, and against the 8.1.2 binary here), where
  FFmpeg's own docs describe it as for servers that "throttle unbounded range
  requests" - so an older build fails at once on the unknown option and
  `_explain` turns that into an explicit "too old" message.
  `ffmpeg_supports_request_size(settings) -> Optional[bool]` (`ffmpeg -h
  protocol=https`; `None` if ffmpeg can't be run) feeds the Doctor's "ffmpeg
  -request_size (YouTube clips)" row.
- **ffmpeg exiting 0 proves nothing about a fetched segment.** When a response
  ends early (connection dropped) ffmpeg treats it as the end of that input, logs
  `Stream ends prematurely ... Error during demuxing: I/O error`, **exits 0**, and
  writes a file whose video is a fraction of the audio (1.4 s of 10 s), which
  the container's own duration (the longest track) hides. Found when one 10 s
  segment on a real 3.5 h 1080p60 stream came back with no video track and
  `fetch_segment` called it a success; reproduced deterministically with a local
  range server that cuts responses short. Three layers, each measured against
  that server with the real ffmpeg: **(1)** `-reconnect 1 -reconnect_streamed 1
  -reconnect_delay_max 5` on every input recovers from drops - clean through 8
  cut responses in a row, because every reconnect resumes from where it got to;
  **(2)** `-rw_timeout 20000000` (`READ_TIMEOUT_US`) turns a stalled read into an
  error after 20 s instead of a hang (and Cancel, only checked when ffmpeg prints
  a progress line, into something that works) - but *alone* it just truncates
  silently, and with reconnect it recovers in about the timeout; **(3)**
  `probe_output(path, settings)` (ffprobe per-track durations, `None` if
  unreadable) and `segment_problem(path, start, end, streams, settings)` check
  the result: each track must exist and reach `min(end, dur) - start` within
  `min(max(0.5, 5 %), half)` of the clip, where `dur` is that track's length from
  the URL - so a clip running past the end of the stream (asked 9 s, 8.0 s exist)
  is not taken for a truncated one. An incomplete result is deleted and fetched
  once more with fresh URLs, then `fetch_segment` raises "YouTube gave back an
  incomplete segment (...) twice in a row" and leaves no file. Faults reconnect
  can't repair (a server that answers with no body, or a source shorter than
  its header promises) exercised layer 3 end to end.
- **Always re-encoded: a stream-copy ("fast") mode was measured and
  deliberately not built.** On the real 720p30 test video keyframes were 3.7-7 s
  apart, a copy carried hidden pre-roll frames and edit lists that surfaced as
  dts collisions at concat seams, video and audio (separate inputs seeked
  independently) gave a silent lead-in, and ffmpeg backs `-ss` off ~0.13 s on
  B-frame streams. Re-encoding is frame-exact, needs nothing special for two
  inputs, and joined cleanly (monotonic DTS at the seams, exact durations).
  Measured speed: on a 720p30 talking-head test video ~9-10x realtime with
  x264 `slow` crf 18 (eight 62.5 s segments compiled in 45.8 s). On the user's
  real 3.47 h **1080p60** stream (H.264 299+140, a 5.3 GB video track) a 10 s
  cut at the `cut.*` defaults (veryfast/crf 20) took 1.5-6 s including the
  network (cold vs warm CDN; 2.5-21 MB output), a 30 s segment at `medium`/crf
  20 took 17.6 s (~1.7x realtime), and a 45 s segment at the compile defaults
  (`slow`/crf 18) took 98 s (~0.5x realtime): **at 1080p60 a compilation is
  encode-bound**, so `compile.preset` is the knob. The first two fetches ever
  made on that stream took 44 s and 36 s and the second came back broken (the
  file above); none of the ~16 later ones did.
- If YouTube ever stops serving progressive-https formats to yt-dlp,
  `resolve_streams` fails and there is no fallback: the full-video download
  escape hatch the original plan sketched was not built.

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

## The edit engine (clipbot/edit*.py, stages/edit.py)

A *spec* (`editspec.py`) says what the video is; `edittimeline.compile_timeline`
works out when everything happens; `editfilters.py` writes the ffmpeg graphs;
`stages/edit.py` runs them. Same split as `reelspec`/`reel.py`, same reason: the
CLI validates and the renderer builds through ONE implementation.

**The invariant that matters: positions are *source seconds*, durations are
*output seconds*.** A clip's `in`/`out`, a speed key's `at`, a camera key, an
overlay's `at`, a marker, an fx `at` are all seconds on the source's own clock
(the same clock as the transcript and `killfeed.json`); `dur`, `in_dur`, a
transition are what the viewer sees. So a callout lands on the kill whether or
not a slow-motion ramp stretched it, and trimming `in` never walks an effect off
its footage. Anything that hangs off *another clip* (music start, an SFX) is an
anchor `{clip, marker | at | from:"end", offset}` resolved after the timeline
exists - never an absolute time, which breaks when an earlier clip changes length.

- **editspec.py** - `normalize(spec)` fills every default and raises
  `SpecError` naming the field (`spec.clips[3].speed[1].rate: ...`);
  `to_json` output loads back unchanged (a test pins it; the output block's
  `faststart`/`preview` keys are accepted and ignored for that reason). Clips are
  `type: clip` (`src,in,out,speed,interp,freezes,camera,grade,fx,overlays,markers,
  audio,framing,transition_in,tags`) or `type: card` (`dur,bg,overlays`). `fx`
  reuses fxspec's effect shapes but only `FX_ALLOWED=(punch, shake, flash, sfx,
  sticker, text)`; ids are auto-assigned. Limits: 400 clips, 12 speed keys, **24
  camera keys** (raised from 12: a four-kill run needs ~16), 24 overlays, 24
  markers. `PROFILES` (`punchy|balanced|restrained`) are taste knobs - the pacing
  gap, default transition, punch zoom, slow-mo rate, pre/post roll, SFX density -
  not limits. `OUTPUT_PRESETS` follow YouTube's upload guide (H.264 High, yuv420p,
  2 B-frames, closed 0.5 s GOP, AAC 48 kHz, faststart): `youtube_1080p60/30`,
  `shorts_1080x1920`, `square_1080`, `portrait_4x5`, `preview_540p`.
- **edittimeline.py** - `build_pieces(clip)`: a speed ramp becomes pieces of
  constant rate, one per `RAMP_STEP` = 0.1 source seconds (the picture needs the fine steps); `src_to_out`, `compile_timeline`
  (durations land on whole frames; an xfade overlap is capped at half of either
  clip), `pacing()` (the hard gate borrowed from i-hate-editing: any stretch with
  nothing new on screen longer than the profile's `max_gap`, a dead opening, an
  overlay up < 1 s or > 4 s, too many SFX), `chapters()` (clips tagged `chapter`,
  cards, >= 10 s apart, 0:00 forced).
- **editfilters.py** - per-clip graph (`build_clip_graph`), in this order: retime
  -> fps conform -> canvas (`fit|fill|blur`) -> grade -> camera -> overlays ->
  flash -> transition fades -> exact-length `trim`. **One ffmpeg input per
  retiming piece** (`-an -ss -t -i`), not one input split N ways - `concat` queues
  every later branch's frames, which was gigabytes for a 40-piece graph. Audio is
  ONE input per clip: a ramp is a single `atempo` group at its average rate
  (`audio_groups`), because `atempo` loses ~25 ms per instance and a chain per 0.1 s
  piece dipped at every boundary; the source is padded 0.07 s and trimmed back to
  the exact planned length. `atempo` takes 0.5-2.0 so `atempo_chain` chains it.
  xfade needs matching timebases (`fps=,settb=AVTB,setpts=PTS-STARTPTS` on every
  node) and a final `trim=end_frame` (it comes out a frame long). Easing
  expressions (`ease_expr`, `keyed_expr`) have a test that evaluates them against
  `editspec.ease_value`. 30 ms audio fades at every cut (`audio_edges`).
- **editoverlay.py** - `render(item, canvas, out_path, font, icon)` draws a
  `text`/`callout`/`image` overlay to an RGBA PNG at 2x supersampling; the PNG is
  cached by `fingerprint()` (content + font + icon mtime). A callout is an accent
  bar + the game's own weapon icon + headline + sub line.
- **editaudio.py** - `find_peak` (5 ms RMS window, not the single biggest sample)
  and `sfx_start`: an SFX is placed so its *peak* lands on the hit (`lead` 0.05 s
  early), not its start; `detect_beats`/`snap_to_beat` (spectral-flux autocorrelation;
  returns no beats rather than a guess); `click_score` (largest sample jump vs
  local RMS - `editqa.CLICK_LIMIT` flags a join that pops).
- **editqa.py** - `verify(ffmpeg, ffprobe, render, timeline, master, out_dir,
  deep=True)` -> `{ok, checks[{name,status,detail}], artifacts}`: A/V length (picture
  vs sound within 1.5 frames), length vs plan, loudness vs target (+-1.5), true peak,
  black / frozen / silent stretches (**planned title cards are exempt** - a mostly-
  dark still card is by design), clicks at joins, pacing. Writes `verify.json/.md`,
  a `loudness.png` (short-term RMS with cuts and events marked), `joins_NN.jpg`
  contact sheets (a frame just after every join) and `moments_NN.jpg` (a frame
  `MOMENT_LAG`=0.6 s after every marker, <= 64: where callouts are up). `ok` is false only on `fail`.
- **editdeliver.py** - `build_package(...)`: `VARIANTS` (`shorts` 1080x1920 <= 180 s,
  `square`, `portrait`) re-frame the master with the blur-fit canvas and **copy the
  audio** (a hero platform deserves a real `edit render --preset`); thumbnails are
  candidate frames 0.45 s after each marker, never within 0.3 s of a join, scored
  by Laplacian sharpness x exposure, >= 5 s apart, JPEG <= 2 MB; `chapter_lines`
  checks YouTube's three conditions (starts 0:00, >= 3, each >= 10 s);
  `title_options`; writes `post.md/json` and `manifest.json` (sha1 of everything).
- **edittemplates.py** - `kill_montage(kills, sources, ...)` -> spec dict.
  `kills_from_killfeed(data, alias, root)` adapts `killfeed.json`. Kills within
  12 s share a clip; clips are chaptered HEADSHOTS -> 50 m+ -> 100 m+ -> MULTI-KILLS
  -> NADES (`order=chapters`; plain kills have no chapter and are left out - use
  `chronological` for all of them); each kill gets a marker, callout (`DOUBLE KILL`
  builds across a clip), punch zoom (max 4 per clip), flash on nades/headshots, a
  slow push-in across any lead-up or gap longer than 0.8 x `max_gap` (so the
  montage passes its own pacing gate - a test asserts it), and an optional
  slow-mo ramp (`slowmo=none|nade|special|all`). Hand-editing the output is
  expected: it is a first draft that is structurally right.
- **edittaste.py** - `work/_edits/taste.json`: `add/retire/active`. Nothing in the
  renderer reads it; whoever *writes the spec* does (see the skill), because a
  taste is a judgement and belongs in the spec where it can be seen.

Edit **skill**: `.claude/skills/clipbot-edit/` (SKILL.md, HARD-RULES.md,
`rules/*.md`) holds the working method and the rules, each with the failure that
produced it; read it before cutting an edit. It adapts the rules-from-failures,
verify-before-present and pacing-gate ideas of github.com/ranahaani/i-hate-editing
(MIT).

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
- Route groups (see `app.py` for the full list): pages (`/` - a redirect to
  the platform tab you used last, via the `clipbot_platform` cookie the two
  library pages set, YouTube on a first visit; `/kick`; `/youtube`;
  `/w/{slug}`, `/w/{slug}/review`, `/w/{slug}/compile`); channel browsing (`GET
  /api/kick/{channel}/vods` — wraps `download_stage.list_channel_vods`,
  stamping each result with `slug`/`workspace_exists` via `slug_for_url` so
  the library page's "Browse a channel" picker can show "Open" instead of
  "Download" for a VOD already pulled down; backs clicking a listed VOD
  straight into a `POST /api/workspaces` instead of requiring its URL to be
  pasted in; and `GET /api/youtube/{handle}/streams` — wraps
  `youtube_stage.list_channel_streams` in a worker thread, stamps the same
  `slug`/`workspace_exists`, caches per `(handle, limit)` for 10 minutes unless
  `refresh=1` (a listing spawns yt-dlp and asks YouTube, and repeated automated
  requests are what bot checks look for), and answers 502 with the actionable
  message when yt-dlp fails); workspaces (`GET/POST /api/workspaces` — `POST`
  takes a Kick or YouTube URL through `platforms.parse_url` (400 with a useful
  message for a YouTube channel/playlist/Short; a bare 11-character video id is
  accepted only when the body says `platform: "youtube"`, since it is otherwise
  ambiguous), stores the canonical URL and submits a `pipeline` job (or, with
  `action: "upload"` and an optional `privacy`, an `upload` job - the "To YouTube"
  button; checked before the workspace is created, so a refusal leaves nothing
  behind, and refused for a YouTube URL or when `youtube_auth.status` isn't
  authorised); a kick.com
  URL the slug regex doesn't recognise keeps the route's old leniency; `GET`
  entries carry `platform`, `source_mode` and `video_id`); clips (`GET/POST
  /api/workspaces/{slug}/clips`,
  **`PATCH .../clips/{clip_id}`**
  — this is the route `specerror.py` references: it catches `KeyError` →
  404 and `ValueError` → 400, so a bad reel/effects spec surfaces as a
  clean 400 with the validator's message); compilations (`GET/POST
  /api/workspaces/{slug}/compilations` — the `POST` is a whole-list
  replace via `compilations.upsert`, the compile page's own editor Save;
  `POST .../compilations/{name}/segments` — additive, `compilations.
  add_segment`, what the review page's "+ Compilation" clip action calls;
  `DELETE .../compilations/{name}` — also purges the rendered `.mp4` and
  scratch dir, `KeyError` → 404); transcript (`GET .../transcript`,
  Devanagari — and `GET .../captions`, the Hinglish transliteration if
  `clipbot transliterate` has been run; both return `{present, segments}`,
  and `captions.json`'s segments share `transcript.json`'s segment `id`s
  exactly, so a frontend can index one by the other to swap a transcript
  row's displayed script without re-deriving offsets — this is what backs
  review.html's and compile.html's Dev/Hin transcript toggle, hidden
  whenever `captions.json` doesn't exist for that workspace); chat (`sync`,
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
  (`POST .../jobs`, `GET /api/jobs`, cancel — `kind: "compile"` takes a
  `name`, same shape as `kind: "reel"` taking `clip_ids`); `GET /api/doctor`
  (diagnostics: tool resolvability, API key presence, CUDA device count,
  free disk, Python version, plus `youtube_stage.health_checks` - yt-dlp
  version/age and the JS runtime - run in a worker thread since they shell
  out; `POST /api/doctor/youtube` is the on-request network probe, and the
  Doctor dialog's "Test YouTube" button calls it; an "ffmpeg -request_size
  (YouTube clips)" row from `ytsegments.ffmpeg_supports_request_size` fails on
  an ffmpeg older than 8.1, and is omitted when ffmpeg can't be run at all; a
  "YouTube upload (OAuth)" row from `youtube_auth.status`, failing only for a
  client that is present but not yet authorised);
  media (video/clip/reel/**compilation**
  serving with `?download=1`, manifest CSV download — a compilation's file
  is always `<name>.mp4`, so the name doubles as the lookup key, no
  `compilations.json` read needed on the media route); and `GET
  /api/events` (Server-Sent Events, with `Last-Event-ID` replay — event
  kinds include `job`, `progress`, `log`, `workspace`, `clips`,
  `compilations`, `preview`, `library`).
- `external_activity(ws)` detects a **CLI-driven** job the dashboard didn't
  start itself: partial download files (`.part`/`.ytdl`), or (Windows-only)
  a `wmic process ... get commandline` scan matching `clipbot` + the
  workspace's video id/slug (case-insensitively - YouTube ids are
  case-sensitive but slugs are lowercased - using `state.video_id` when
  recorded) for one of `transcribe`/`analyze`/`cut`/
  `audio`/`download`/`compile`/`run` — needed for stages like transcribe
  (and compile) that write no partial file to detect otherwise.
- **Platform tabs**: `base.html`'s topbar carries a Kick | YouTube tab strip;
  every page passes `platform` so the tab you're inside stays lit (the library
  pages from their route, workspace pages from `ws.platform`). `library.html`
  is the Kick tab and `library_youtube.html` the YouTube tab (paste a link or
  id, browse a channel by handle, a grid of that platform's workspaces); each
  filters `GET /api/workspaces` by `platform` and both render cards through the
  shared `workspaceCardHtml()`/`stageDots()` in `app.js`. A workspace with no
  `platform` is Kick, so nothing existing moves tabs.
- **`stage_states(ws)` is platform-aware**: a YouTube workspace gets Fetch
  audio → Extract audio → Transcribe → Find clips → (Hinglish captions,
  Speaker diarization) → "Fetch approved clips" (the `cut` job, ready once at
  least one clip is approved, done when the `cut` stage is marked) and *no*
  chat/reel/cleanup rows - nothing gates on `has_video`. Kick's list is
  unchanged except for an "Upload to YouTube" row (key `upload`, before "Delete
  VOD"): ready whenever a URL is recorded, deliberately not gated on `has_video`
  because the VOD can be relayed without a download. `workspace.html` gives it a
  visibility picker, confirms before publishing and asks harder before a second
  upload, and hides the Kick-only quality picker for
  YouTube, says the video plays from YouTube, and words the empty clip list as
  "No clips fetched yet...".
- **Player source**: `page_review`/`page_compile` pass `_player_source(ws)`
  into the template - `{"kind": "youtube", "video_id", "duration"}` when
  `ws.source_mode() == "embed"`, else `{"kind": "local"}` (which is also a Kick
  workspace whose VOD was deleted, so it keeps its old "no video" message). In
  embed mode the templates render a `div.yt-frame` instead of the `<video>`,
  skip the reel panel / crop layer / effects lane / FX-library dialog, don't
  load `reel.js`/`fx.js` at all (review.js already guards its two calls into
  them with `window.X` checks), and add a "↗ YouTube" link that opens the
  video at the playhead. "Cut approved" and the compile page's Render stay
  enabled - they run the same `cut`/`compile` jobs, which fetch just those
  seconds from YouTube (`ytsegments.py`); only a tooltip says so.
- **`static/player.js`** is the adapter behind that: `createPlayer(el,
  source)` returns the native `<video>` untouched for `local` (so Kick pages
  are unchanged and never load any third-party script), and a `YouTubePlayer`
  for `youtube` with the same surface review.js/compile.js already use
  (`currentTime` get/set, `duration`, `paused`, `playbackRate`, `hidden`,
  `play()`, `pause()`, `addEventListener` for `loadedmetadata`/`timeupdate`/
  `play`/`pause`/`error`, plus `errorMessage` and `watchUrl(t)`). Behaviours
  worth knowing, each measured against the real player: there is no
  `timeupdate` event, so it polls `getCurrentTime()` every 200 ms; a
  transparent **click shield** over the iframe keeps keyboard focus in the
  page (a cross-origin iframe would swallow space/I/O/[ ] the moment you
  clicked the video) and toggles play/pause; **until the first play YouTube
  remembers only the latest `seekTo` and `getCurrentTime()` stays stale**, so
  seeks made before the first play are held in `_pendingSeek` and applied by
  `play()`; `seekTo` from an **ended** video restarts playback, so a seek while
  not playing is followed by `pauseVideo()` (from plain paused it does not
  restart); a just-issued seek is trusted over the polled time for 500 ms; the
  iframe carries an explicit `referrerpolicy="strict-origin-when-cross-origin"`
  (an embed with no Referer answers error 153); `duration` is the workspace's
  audio-probed length (the clock the transcript lives on) with YouTube's own
  figure only as a fallback, and `loadedmetadata` fires straight away from it
  so the timelines and transcript still work when the player itself is broken.
  API errors 101/150 (embedding disabled) and 153 map to messages saying what
  to change. The API script (`youtube.com/iframe_api`) is loaded only when a
  `youtube` player is created - a third-party script in an unauthenticated
  dashboard origin never runs on a Kick/local page. A frame-step
  (`step(1/30)`) turned out to be frame-accurate on the test video.
  `tests/js/player_adapter.test.js` pins all of this against a fake
  `YT.Player`.

### jobs.py — JobRunner / EventBus

- **Three worker lanes**: `HEAVY_KINDS = (download, audio, transcribe,
  analyze, diarize, pipeline, reel, compile)`, `LIGHT_KINDS = (cut, chat,
  transliterate, manifest, cleanup, waveform)`, `NET_KINDS = (upload,)`, each on
  its own thread — a 3-second clip cut never queues behind a 90-minute transcribe,
  and chat harvest (must run before Kick's retention expires it) never
  queues behind an x264 render. `transliterate` is light for the same
  reason as `chat`: a handful of batched Claude calls, not CPU/GPU-bound.
  `diarize` is heavy for the same reason as `transcribe`: CPU-bound (no CUDA
  on this machine) and can run long on a multi-hour VOD. `compile` is heavy
  for the same reason as `reel`: every segment is a full x264 re-encode.
  `upload` has its own lane because it is network-bound for as long as the VOD
  is long (hours): in the heavy lane it would hold up downloads and
  transcribes, in the light lane it would hold up chat harvest, which must not
  wait because chat expires with the VOD.
- Threads, not subprocesses, because none of the actual work is
  Python-CPU-bound (CTranslate2 releases the GIL; ffmpeg/yt-dlp are
  subprocesses; Claude calls are network I/O) and the ~3GB Whisper model can
  stay resident between runs.
- `Job` states: `queued → running → {succeeded, failed, cancelled,
  interrupted}`. **One job per workspace at a time**, enforced at
  `submit()`, to prevent concurrent `state.json` writes.
- `Job.to_dict()` carries progress fields dashboards render generically:
  `fraction`, `phase`, `label`, `eta_seconds`, plus `current`/`total`/`unit`/
  `rate` (the last four added for `download`'s byte-based progress, but
  populated for any stage that passes them through `Progress.update()` —
  `_on_progress` forwards whatever `update()` emits rather than special-
  casing a stage). `unit == "bytes"` is what `app.js`'s `renderJobs()` keys
  off of to show a speed/remaining-size line; other stages' `unit` values
  (e.g. compile's `"segments"`) don't get that line.
- `EventBus` keeps a `deque(maxlen=400)` history with monotonic ids for SSE
  `Last-Event-ID` replay on a reconnecting browser tab.

### Known loose end: `waveform` job kind is unregistered

`jobs.py`'s `LIGHT_KINDS` lists `"waveform"`, and `settings.json` has a
`server.waveform_bins_per_second` setting, but `app.py` never registers a
`waveform` handler — submitting one raises `StageError("Unknown job kind
'waveform'")`. `chatsync.py` reads `audio.wav` directly instead of a
pre-computed `waveform.json`. This looks like a partially-built/abandoned
feature — don't assume a waveform file exists anywhere. (`LIGHT_KINDS` used
to also list `"benchmark"`, with the same unregistered-handler problem but
none of `waveform`'s supporting scaffolding — no `Workspace` path, no
settings, no dashboard purpose; `transcribe --max-seconds` benchmark mode is
CLI-only. Removed outright rather than left as a second dead kind.)

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
transcribe backend,
whisper model/device/language, Claude model, rubric file path).

Sections and the values worth knowing without opening the file:

- **`tools`** — `ffmpeg`/`ffprobe` point at the **vendored** local build
  (`ffmpeg-2026-07-30-git-.../bin/`) because ffmpeg isn't on this machine's
  PATH; relative paths resolve against the project root. (The vendored build is
  gitignored, so a fresh git worktree doesn't have it - set `CLIPBOT_FFMPEG`
  there.) `yt_dlp` is a bare `yt-dlp`; the one on this machine's PATH can't do
  YouTube, so YouTube work needs `CLIPBOT_YT_DLP` pointed at a current install
  (see the youtube.py section).
- **`download`** — `format` pins `height<=720` explicitly ("best" once
  silently returned 160p); `fallback_to_kick_dl: false` (see above);
  `http_chunk_size: "10M"` (yt-dlp bandwidth-throttling-bypass flag,
  sequential only in the installed yt-dlp version — see download.py's
  section for what's actually been measured; `null` omits the flag). All of
  this - `format`, `impersonate`, `http_chunk_size`, `min_height_warn` - is
  Kick-only; nothing in it is applied to YouTube. Nested **`youtube.*`**
  (`stages/youtube.py`, all optional): `audio_format` (`"bestaudio/best"`),
  `keep_source_audio` (`false`: `audio.wav` is the artifact everything reads, so
  the audio download is deleted after extraction), `cookies_from_browser`
  (`null`; set to a browser name if YouTube demands a bot check - Firefox avoids
  Chrome's locked cookie database), `player_client` (`null`; a yt-dlp
  `youtube:player_client` override for when one client breaks), `js_runtime`
  (`null` = yt-dlp's default, deno; `"node"` or `"node:<path>"` selects Node
  22+), `probe_url` (the public video the Doctor's "Test YouTube" asks about),
  `segment_format` (`null` = `ytsegments.DEFAULT_SEGMENT_FORMAT`, the
  progressive-https H.264+AAC selector up to 1080p that cutting clips straight
  from YouTube resolves - override only with another progressive-https
  selector), `request_size` (`1048576`: the bounded HTTP request size ffmpeg
  uses against googlevideo, clamped to 64 KiB-8 MiB in code because a larger
  value was measured to throttle to a crawl). The re-encode quality of those
  clips/segments comes from `cut.encoder`/`preset`/`crf` and `compile.*`, not
  from this block.
- **`chat`** — bot list matched on `sender.slug`, lowercase; full
  `chatrender` style block (fonts, sizes, colors) lives here too.
- **`transcribe`** — `backend: "openai"` (default; `"local"` selects
  faster-whisper instead — dashboard has a per-run picker on the transcribe
  stage button); `language: "hi"` (forced, not auto-detect);
  `low_confidence_threshold: -0.7`; `max_segment_seconds: 30.0`; nested
  `openai.*` block (`model: "whisper-1"`, `api_key_env`, `chunk_seconds:
  1200`, `concurrency: 4`, `max_retries: 3`) only used when `backend` is
  `"openai"`.
- **`analyze`** — `model: "claude-sonnet-4-6"`; `thinking: false` (see
  analyze.py above for why); `effort: "medium"`; `chunk_threshold_minutes:
  90` / `chunk_minutes: 60` / `chunk_overlap_minutes: 8` / `concurrency: 2`
  (windowed analysis for long streams, see analyze.py above); nested
  `signals.*` block (`enabled`, `audio_bin_seconds`, `audio_z_threshold`,
  `chat_bin_seconds`, `chat_z_threshold`) controls the energy/chat-spike
  annotations from `highlights.py`.
- **`transliterate`** — `model: "claude-haiku-4-5"` (deliberately cheaper
  than `analyze.model` — mechanical task, not a judgment call);
  `batch_size: 50`; `thinking: false`.
- **`diarize`** — `model: "pyannote/speaker-diarization-3.1"`,
  `device: "auto"` (same cuda-else-cpu resolution as `transcribe.device`),
  `hf_token_env: "HF_TOKEN"` (names the env var, not the token itself);
  `num_speakers`/`min_speakers`/`max_speakers` all `null` by default (let
  pyannote infer).
- **`cut`** — `pad_start: 1.0`, `pad_end: 1.5`, `re_encode: false`.
- **`compile`** — `min_duration: 0.5`; `encoder: "libx264"`, `preset: "slow"`,
  `crf: 18` **deliberately does not inherit `cut.encoder`/`cut.preset`/
  `cut.crf`** (same reasoning as `reel.preset_x264` below — it's a final
  deliverable); padding reuses `cut.pad_start`/`cut.pad_end` rather than its
  own setting, same choice `reel` already made.
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
- **`upload`** — nested `youtube.*` (`stages/upload.py`, `youtube_auth.py`):
  `client_secret_file` (`config/youtube_client_secret.json`; env
  `CLIPBOT_YOUTUBE_CLIENT_SECRET`), `token_file` (`null` =
  `<work_root>/_cache/youtube_token.json`), `privacy` (`"private"`), `mode`
  (`"auto"`), `title_template` (`"{title}"`), `description_template`,
  `category_id` (`"20"`, Gaming), `made_for_kids` (`false`), `chunk_mb` (`16`;
  about three held in memory in stream mode), `max_retries` (`8`) and
  `stream_container` (`"mp4"`).
- **`killfeed`** — `player` (`"Teck"`), `fps` (`1.0`), `scale` (`3`), `crop`
  (`"620:230:0:440"`, `W:H:X:Y` of the feed in a 1280x720 frame), `chunk_seconds`
  (`1800`, how much video is decoded to PNGs at a time).
- **`edit`** — `segment_pad` (`0.6` s cut either side of each clip so a zoom or
  ramp never runs out of picture), `thumbnails` (`3`).
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
- `tests/test_highlights.py` — `compute_energy_spikes` against a small
  synthetic WAV file (quiet baseline + a loud burst, written directly with
  the stdlib `wave` module), `compute_chat_spikes` against a synthetic
  chat.json dict (including the offset-correction sign convention), and
  `notable_moments`'s graceful-degradation path against a real temp
  `Workspace` with neither `audio.wav` nor `chat.json` present.
- `tests/test_compile.py` — `compilations.json` CRUD/fingerprinting/
  skip-unchanged-or-force behavior (ffmpeg calls stubbed, same reasoning
  cut.py's own untested subprocess calls rely on), plus the cross-stream
  additions specifically: `_normalize_segments`'s source-aware ordering/
  overlap branch (single-slug still sorts+checks globally, multi-slug
  preserves given order and only checks overlap per source), fingerprints
  changing with source `slug` (not just numeric range),
  `render_compilation` resolving segments from two real temp `Workspace`s
  and raising a clear error for a segment naming a workspace with no video,
  and `unrendered_compilations_elsewhere` blocking one workspace's cleanup
  on an unrendered compilation homed in another.
- `tests/test_platforms.py` — the **Kick-slug oracle**: `slug_for_url` must
  agree with a verbatim copy of the pre-YouTube implementation on every
  non-YouTube input (Kick URL shapes plus generic fall-through URLs), plus
  literal golden slugs so a drifting oracle can't hide a drifting slug; every
  YouTube URL shape yielding one id, case preserved in `video_id` but lowercase
  in the slug (and hyphen runs kept, which `slugify` would collapse),
  channel/playlist/Short URLs raising a useful `ValueError`, the bare-id opt-in
  (an 11-character Kick channel name must never parse as a video id),
  `Workspace.for_url` recording platform/video id/canonical URL,
  `source_mode()`, and `source_audio_path()` ignoring yt-dlp scratch files.
- `tests/test_youtube.py` — yt-dlp is never run except in `TestRunYtDlp`,
  which drives a real subprocess (this Python) to prove progress parsing, the
  new-phase-on-reset behaviour and cancellation. Everything else stubs
  `run_yt_dlp` to write the files a real run would: argv shape (no Kick flags,
  optional flags only when configured), the skip/force rules (including
  "`force` never hands back `audio.wav`"), the live-stream refusal, failure
  hints, `drop_source_audio` never deleting a caller's file, the audio-stage
  handoff and `acquire()` routing, channel-listing parsing/validation, and the
  Doctor's version/JS-runtime checks. Mutation-checked: dropping the early
  return, the ownership check, or leaking `--impersonate` each fail a test.
- `tests/test_youtube_segments.py` — neither yt-dlp nor ffmpeg is run: both are
  stubbed. `_streams_from_info` (video+audio, muxed, segmented/missing streams
  refused; each track's `dur=`), `request_size` clamping, the `segment_argv`
  shape (bounded requests, reconnect and read timeout on **each** input, `-ss`
  before each, maps, all `-i` before `-t`, CRLF-joined `-headers` with the user
  agent passed separately), `probe_output` (per-track durations, never the
  container's; unreadable -> `None`), `segment_problem` (a truncated video under
  a whole audio track, missing tracks, encoder-rounding slack, a clip past the
  end of the stream judged against `dur=`),
  `resolve_streams`, `StreamResolver` (lazy, one resolution, re-resolve near
  expiry, tag), `fetch_segment` (one retry on a rejected URL and no more, an
  incomplete result deleted and fetched again then an error with no file left,
  cancel propagation without a retry, old-ffmpeg message), then the real
  `cut_clips`/`render_compilation` against a YouTube workspace with only
  `fetch_segment` stubbed (records the clip, skip-unchanged vs `force`, a
  better resolution re-cuts, a failed clip is marked while the rest still cut,
  cancel removes the partial file, mixed YouTube/local and differently shaped
  videos refused). The **fingerprint invariants** live here: a Kick clip's cut
  fingerprint string and the local compile fingerprints (against a verbatim copy
  of the old implementation plus golden values) must not move, and a Kick
  workspace without a video keeps the original "No video" error. The existing
  `test_compile.py` passes unmodified alongside it.
- `tests/test_youtube_upload.py` — `FakeYouTube`, an in-process HTTP server that
  speaks the resumable protocol and can lose a chunk, store one without
  acknowledging it, acknowledge only part of one, 401 a token, expire the
  session or refuse unknown length. Covers byte-exact round trips (file and
  unknown-length stream, including an exact chunk multiple and a sub-chunk
  source), bounded retries, resume from a saved session, the spool fallback
  losing no bytes, ffmpeg dying never confirming a video, idempotence, and the
  `-bsf:a aac_adtstoasc` rule. `TestRealFfmpegRelay` remuxes a generated HLS
  playlist from a local server with the real ffmpeg and ffprobes the result
  (skipped without ffmpeg; set `CLIPBOT_FFMPEG`).
- `tests/test_youtube_auth.py` — PKCE, the whole loopback flow against a fake
  token endpoint, a forged `state`, declined consent, refresh/rotation/
  `invalid_grant`, and `status()` never touching the network.
- `tests/test_upload_server.py` — upload on its own lane, the stage row, the
  Doctor row, `POST /api/workspaces` with `action: "upload"` (refused before
  anything is created; submits `upload`, not `pipeline`), and the job handler.
- `tests/test_utils.py` — `resolve_tool`: a value that can't be a path (stray
  quote or control character in `CLIPBOT_YT_DLP`, which raises `OSError` from
  `Path.is_file()` on Windows) reads as "not found" with its hint, never a
  traceback or a dashboard 500; wrapping quotes/whitespace are stripped.
- `tests/test_player_js.py` + `tests/js/player_adapter.test.js` — the only JS
  tests in the repo: a dependency-free Node script drives `static/player.js`
  against a fake `YT.Player` that models the measured YouTube behaviours
  (stale time before first play, seek-from-ended restarts, no timeupdate).
  Skipped without Node. Mutation-checked for the pending-seek path, the stale
  tick guard, the pause-after-seek, and the referrer policy.
- The edit engine, one file per module (all under `tests/`): `test_editspec`
  (defaults, every rejection names its field, `normalize(to_json(x)) == x`),
  `test_edittimeline` (retiming math, anchors, pacing gate, chapters),
  `test_editfilters` (golden fragments; **easing expressions evaluated numerically
  against `editspec.ease_value`**; a real-ffmpeg smoke test that a plain, a
  slow-mo-ramp+camera+flash and a freeze clip each come out exactly the planned
  frame count with sound the same length), `test_editaudio`, `test_editoverlay`,
  `test_editqa` (detectors shown media with a known flaw: black, silent, frozen),
  `test_editdeliver` (incl. a real package build), `test_edittemplates` (+ taste;
  asserts the montage passes its own pacing gate and camera keys rise),
  `test_edit_stage` (a full render of an xfade + card + ramp + music + SFX edit on
  synthetic media: length, A/V sync, loudness near target, **a re-render reuses
  every clip**, one changed clip re-encodes alone, a render never overwrites
  spec.json), `test_editcli`, `test_killfeed` (synthetic OCR words, icons). The
  ffmpeg-using tests skip when no ffmpeg resolves.
- Run with `python -m unittest discover -s tests` (no `pytest` installed in
  this environment as of this writing; no CI config in this repo either).
  `tests/` is not a package, so `python -m unittest tests.test_x` fails; pass
  `-p test_x.py` to `discover` to run one file.

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
  `kick_duration` (and, for YouTube, `youtube_duration`) from yt-dlp metadata
  is informational only and is never allowed to overwrite it - it may only
  seed `duration` while none has been probed yet.
- **Kick slugs are directory names and must never change.**
  `platforms.slug_for` reproduces the pre-YouTube `slug_for_url` byte for byte
  for Kick, and `tests/test_platforms.py` compares it to a copy of the old
  code. A YouTube slug is `yt-<lowercased id>`, but the id is case-sensitive:
  the true one lives in `state.json` (`video_id`) and must never be re-derived
  from the slug.
- **A workspace's platform is `state["platform"]`, and absent means Kick.**
  `Workspace.source_mode()` (`local`/`embed`/`none`) is derived from what is on
  disk, never stored, so it can't drift from reality.
- **Kick's yt-dlp workarounds (`--impersonate`, `--http-chunk-size`) are never
  applied to YouTube**, and YouTube's are never applied to Kick.
- **Local fingerprints must stay byte-identical when YouTube support is
  involved.** `cut._fingerprint`, `compile._segment_fingerprint` and
  `compile._compilation_fingerprint` append a YouTube tag only when one is
  given; a Kick/local clip's or segment's fingerprint (and every scratch file
  cached under it) is unchanged, so no existing render is redone. Pinned by
  golden values plus a verbatim copy of the old code in
  `test_youtube_segments.py` - the same discipline as the reel filter graph.
- **Segments cut from YouTube are always re-encoded, and ffmpeg always gets
  bounded requests** (`-request_size N -multiple_requests 1`, N clamped to
  64 KiB-8 MiB). Without them googlevideo throttles the read to ~31 KB/s and a
  cut appears to hang; a stream-copy mode was measured and rejected (see the
  ytsegments.py section) - don't add one without re-measuring keyframe spacing,
  audio lead-in and dts at the concat seams. **A fetched segment is only
  accepted after its own tracks are checked** (`segment_problem`): ffmpeg exits 0
  after a dropped connection with a truncated video, so never treat its exit
  code as the verdict.
- **The YouTube IFrame API script only ever loads on an embed-mode page.** The
  dashboard has no auth and a third-party script in its origin can call every
  mutating route, so it must never run on a Kick/local page (`createPlayer`
  returns the native `<video>` untouched for those).
- **Uploading to YouTube is opt-in and private by default.** `upload` is never
  part of `run` or the `pipeline` job, defaults to `private`, is idempotent (a
  second run is a no-op without `--force`) and every dashboard entry point
  confirms first. Credentials are the user's own; the client secret and token
  are gitignored and must never be committed or logged.
- **A relayed upload must never confirm a truncated VOD.** The source is verified
  (ffmpeg exit status, duration shortfall) *before* the final chunk - the one
  that completes the upload - is sent. Anything that changes how
  `upload_stream` reads ahead must keep that ordering.
- **`upload`'s spool lives in `_upload/`, never the workspace root**:
  `Workspace.video_path()` scans the root for video extensions.
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
  (Current yt-dlp itself needs 3.10+ for YouTube, but ClipBot only shells out
  to its binary, so that never raises this project's floor - see the
  youtube.py section.)

- **Edit positions are source seconds, durations are output seconds** (see "The
  edit engine"). Anything hanging off another clip is an anchor, never an absolute
  time. Don't convert a position to "seconds into the video".
- **A render never overwrites `spec.json`** - it writes `render/<name>.<tier>.spec.json`.
  (A preview once replaced the author's spec with its own 540p output block.)
- **The edit engine passes graphs to ffmpeg with `-/filter_complex <file>`.**
  `clipbot/stages/reel.py:112` still uses `-filter_complex_script`, which ffmpeg 8
  (the vendored build) no longer has - reel rendering is broken against it until that
  one line is changed. Not fixed here.

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
  same `transcript.json` shape as `stages/transcribe.py`, then add a branch
  to that module's `transcribe_audio()` dispatcher (see
  `stages/transcribe_openai.py` for the working example: OpenAI's API is
  the current default backend) — nothing downstream of `transcript.json`
  needs to change.
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
- ...add support for another streaming platform → `clipbot/platforms.py`
  (URL parsing + slug) + a branch in `download.acquire` + a stage module like
  `stages/youtube.py` + a platform branch in `server/app.py`'s `stage_states`
  and `_player_source` + a library tab (`base.html`, a `library_*.html`) +
  whatever player `static/player.js`'s `createPlayer` should return. Pin the new
  slug shape in `tests/test_platforms.py`.
- ...work on the embedded YouTube player → `clipbot/server/static/player.js`
  and its Node tests (`tests/js/player_adapter.test.js`, run via
  `tests/test_player_js.py`); check any new YouTube behaviour against the real
  player first, as the file's header does - several things it handles were
  not what the docs suggest.
- ...cut clips or compile segments from a YouTube video without downloading
  it → already wired: `cut_clips`/`render_compilation` call
  `ytsegments.fetch_segment` for a workspace with no video on disk. Change how
  a range is fetched in `clipbot/ytsegments.py` (read its section first: the
  bounded-request options are the difference between ~1 s and a hang), and keep
  the local fingerprints byte-identical.
- ...diagnose "YouTube doesn't work" → the dashboard's Doctor dialog
  (`youtube_stage.health_checks` + the "Test YouTube" button →
  `probe_youtube`, plus the "ffmpeg -request_size" row for clip fetching), then
  `explain_failure()`'s hints; the raw yt-dlp output is in
  `work/<slug>/logs/youtube-metadata.log` and `download.log`, and a clip or
  segment fetch's ffmpeg output in `logs/cut-<clip id>.log` /
  `logs/compile-<name>-<NNN>.log`.
- ...understand a workspace's on-disk state → `clipbot/workspace.py`'s path
  properties are the authoritative list; `state.json`'s `stages` dict says
  what's been completed.
- ...understand the review/approval state machine → `clipbot/review.py`
  (`reconcile()` is the interesting one: it re-matches a fresh rubric run's
  candidates against existing human-edited clips by time-overlap).
- ...assemble a landscape supercut from several non-contiguous VOD ranges,
  optionally spanning multiple streams (e.g. a curated YouTube highlights
  video) → `clipbot compile --range [slug:]start,end,label ...` /
  `clipbot/stages/compile.py` + `clipbot/compilations.py` (also reachable
  from the dashboard at `/w/{slug}/compile` for review/render, not
  cross-stream authoring — see compile.py's section above) — not
  `clipbot cut`, which only ever produces separate per-clip files.
- ...upload a Kick VOD to YouTube, from disk or relayed with no download →
  `clipbot upload` / `clipbot/stages/upload.py` (protocol in `ytupload.py`,
  login in `youtube_auth.py`, one-time `clipbot youtube-auth`). To upload clips,
  reels or compilations, give `upload_vod` a different source file -
  `ResumableUpload.upload_file` is already generic.
- ...add a new full dashboard page (not just a route) → a new template
  extending `base.html` (gets the shared topbar/job-panel/SSE for free) +
  a dedicated `static/<page>.js` if it's non-trivial (see `compile.html`/
  `compile.js` for a from-scratch example built by adapting review.js's
  timeline/transcript techniques rather than including review.js wholesale)
  + a page route in `app.py` + a nav link from wherever makes sense
  (`workspace.html` for a per-workspace page).

- ...make an edit (retiming, callouts, music, mastering) from clips of any
  source → write a spec (`clipbot edit new` for a kill montage, else by hand to
  `work/_edits/<name>/spec.json`), then `edit validate` -> `edit render --preview` ->
  `edit render` -> `edit verify` -> `edit deliver`. Read `.claude/skills/clipbot-edit/`
  first.
- ...add a new kind of edit template → `clipbot/edittemplates.py` (a pure function
  returning a spec dict; validate it with `editspec.normalize` in a test and compile
  it with `edittimeline.compile_timeline` + `pacing`) + a `--template` choice in
  `editcli.py`.
- ...add a new filter/effect to the edit engine → `editspec.py` (schema) +
  `editfilters.py` (the builder, in the order documented there) +
  `edittimeline.py` if it has a time-mapped position + a golden-string test and, if
  it is a moving value, a numeric test in `tests/test_editfilters.py`.
- ...read another game's kill feed → `data/windows_ocr.ps1` is generic;
  `killfeed.py`'s row grammar (`observe`) and `LEFT_MARGIN`/`TOP_LABEL_Y`, and the
  `killfeed.crop` setting, are Wardogs-specific. Run `scan` on a short range and
  check `killfeed_icons/` by eye before trusting the numbers.

## Keeping this file honest

There's a project skill, `clipbot-docs-sync`
(`.claude/skills/clipbot-docs-sync/SKILL.md`), whose job is to update this
file whenever a change to the code would make something written here wrong.
If you make a change that touches anything documented above (a new stage, a
new route, a changed schema field, a changed threshold/constant, a new
settings key, a new job kind) and you're not already running that skill,
invoke it (or at minimum, hand-edit the relevant section here) before
calling the change done — this file is only useful if it doesn't lie.
