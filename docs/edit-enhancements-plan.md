# Edit enhancements plan: from "assembles a good cut" to "a complete editor"

Written 2026-10-23, after Teck Mish Mash #1 (`work/_edits/teck-mish-mash-1`, commit `ebbea2f`).
Goal: Claude delivers a YouTube-ready final with a high-production-value look,
through an iterative loop that you can steer, and gets better with every video.

---

## 0. Revision after your v2 feedback (2026-10-24)

You watched v2 and said three things. Here is what each one turned out to be, whether the plan
above would have fixed it, and what changes.

### 0.1 "Quality is fucked in the clips at 1:45; the previous version was a lot better"

**Cause (measured, mine):** 1:45 is clip `c2_09`, one of nine "The Base" clips from the Sep 24
YouTube stream. In the v2 render that stream resolved to **YouTube format 18, 640x360 @ 30 fps**
instead of format 299+140 (1920x1080 @ 60). The render log said so
(`format 18, 640x360 @ 30 fps`, plus "source 'y0924' runs at 30.00 fps") and nothing stopped it. The
cause was the `player_client=web_embedded` setting I used to get past YouTube's 429 on 21 Oct: it
returned 1080p60 that day and **only 640x360** on 24 Oct (checked just now: the default client
offers 1080p60 again, `web_embedded` and `mweb` offer 360p). The 360p segments were upscaled to
1080p, which is the mush you saw (mean edge energy 2.79 against v1's 7.13 at 105 s).

**Fixed now:** re-rendered with the default client. All 54 clips measure the same sharpness as v1
(ratio 1.00 on every clip; none under 0.85). New guard: `edit.min_source_height` (720). Under it
the resolver tries the configured client, yt-dlp's default, `web_embedded` and `mweb` and takes the
first that meets the floor; if none does, the render **refuses** and names what each client offered.
Tested, and the tests fail without the guard. My earlier note that `web_embedded` is a reliable
workaround was wrong and is corrected in CLAUDE.md.

**Would the plan have caught it? No.** QC v2 (section H) checked motion, level and captions; nothing
looked at *source quality*, and I shipped v2 without comparing it to v1. Added below (Q).

### 0.2 "The zoom-ins on the camera are wrong when the camera position changes with the stream scene; this needs to be dynamic"

> **Correction, 2026-10-24 (phase 1b):** the cause below was incomplete, and the part I missed was
> worse. The keys ask for a centred zoom, but the filtergraph never delivered one: `camera_chain`'s
> crop read the scaled frame's width back as `iw`, which stays at the first (unzoomed) frame's size
> when the frame size changes per frame, so the crop window clamped to (0, 0) and **every zoom pivoted
> on the top-left corner**. I found it by measuring where a known webcam box landed in a rendered
> frame (0.790 where a centred 1.09x zoom puts it at 0.740). It has been in the engine since the first
> edit; it is fixed, pinned by a test that fails against the old expression, and the clip cache was
> invalidated. Everything below about "centred" is what the keys said; the render did something else.

**Cause (measured):** every one of the 54 clips (30 YouTube, 24 Kick) has camera keys, and **all of
them ask for a zoom around the dead centre (0.5, 0.5)**, up to 1.09x. The stream overlay is not at the centre:
on the YouTube layout the facecam and the channel logo sit against the right edge, on the Kick layout
the cam is at the left, and full-cam scenes are another layout again. A centre zoom therefore crops
the webcam box and the logo off the frame (frame at 1:47: the cam's right border and the poster are cut
by the frame edge), and when the stream switches scene the zoom is aimed at nothing in particular.
Rule 13 ("a webcam is part of the frame") existed, but nothing enforced it for zoom.

**Would the plan have caught it? Only partly.** A5 (a cam box per workspace) is static, but layouts change
inside a stream; B2 (reaction framing) is for chosen scenes only. Neither makes ordinary zooms safe.
Replaced below (S).

### 0.3 "Some clips are great, some not so much; we can do a better job finding clips; parts are missing and don't include the main context kills and the stuff around it"

**Causes (measured / admitted):**
- I picked moments from `candidates.json` descriptions and transcript keyword hits, never from video, and
  set every in/out at a **spoken line**, not at the **action**. Example: the second mortar clip begins at
  "oye mere upar mortar gir gaya", which is the reaction *after* the hit; the hit and the loot-up before it are
  not in it, and the first is nine seconds of Teck retelling one with no footage of it.
- **Kill coverage is thin.** The kill-feed reader has been run on 5 of the 15 streams since Wardogs started
  (Sep 24, 26, 27, 28, 30: 70 kills). The streams with the mortar and helicopter gags (Sep 11, 15, 25) were
  never scanned, and a few that were are partly unreadable (webcam over the feed), so the best kills of the
  run are simply not in the pool. The Receipts chapter used 6 of those 70, each in a 9-16 s window of 6 s
  lead-in and 3 s tail: the kill and nothing of the fight around it.
- Nothing checks a clip for completeness (setup, action, aftermath) and nothing watches a clip before it
  goes in.

**Would the plan have caught it? Only weakly.** A1/A2/E make me better at hearing and let you comment, but
the plan has no step that *builds a moment from its pieces*, ranks the candidates against each other, or
measures whether the pool is complete. Added below (M).

### 0.4 Plan changes

**New workstream Q: source quality.**
Q1 the floor above (done). Q2 `edit verify` records the resolved format for every source and flags any
clip whose sharpness is under 85% of the same clip in the previous version or under the source's own
expectation. Q3 `edit compare NAME v1 v2`: per-clip sharpness, loudness and duration deltas and a
side-by-side sheet of the clips that changed (the script that found this is the seed). Q4 a one-line
"sources" table in every report: stream, resolution, fps, upscaled yes/no. Q5 re-resolve health: log which
player client worked and warn when the fallback ladder was used.

**New workstream S: scene-aware framing (replaces A5 and widens B2).**
S1 *Scene map*: per stream, classify every second as `game+cam(left|right|corner)`, `full-cam`,
`menu/loading/black`, `map/inventory`, from sampled frames (colour-box finder for the webcam border, face
detector, edge statistics). Output `scenes.json` with the cam rectangle and the safe area per segment.
S2 *Safe zoom*: a camera key's focus and maximum zoom are computed from the scene's overlay rectangles, so
the zoom can never crop the cam, the channel logo or the HUD corners; if a punch cannot be done safely it is
dropped, not forced. S3 *Targeted punch*: the focus comes from where the action is (crosshair/hit marker
for a kill; the face for a reaction in a full-cam scene) rather than the centre. S4 `verify` check
"overlay clipped": every stream-overlay rectangle in the source must be fully inside the output frame
wherever the clip has no authored framing. S5 layout-aware lower-thirds (they already avoid the cam; make it a
rule the verifier checks).

**New workstream M: moment engine (replaces the "choose by description" step).**
M1 *Scan every stream's kill feed*, starting with the Wardogs era (10 unscanned streams; about 4 minutes
of OCR per hour of video (1 s of work per 15 s)). M2 *Arc builder*: from a seed (a kill, a laugh peak, a transcript hit) grow a
window to a full arc: lead-up back to the first gunfire or contact (audio onset + kill-feed + hit markers),
the action, then the aftermath (reaction, loot, revive) until the audio settles; merge arcs that touch.
M3 *Rank*: a score from kill value (distance, headshot, nade, multi), reaction (laugh/shout peak from A2),
chat, and the author's own approvals, so the pool is ordered by what a viewer would care about. M4 *Evidence
sheet per candidate*: contact sheet of 8 frames, the transcript lines, the audio-event bars and the numbers, so
choosing is done from what is on screen. M5 *Completeness check*: end-of-match scoreboard reader (kills,
deaths) to know how many kills a game had against how many the feed reader found; warn when coverage is
under 80%. M6 *Context rules* in the skill: a kill clip carries at least ~10 s of lead-up and ~5 s of
aftermath unless the author marks it a "cutaway"; a joke clip carries its setup.

**Re-ordered roadmap.** Phase 0 is done (and showed the three problems). The next phase is now **Phase 1:
fix what you saw**, before ears and captions:

| Phase | Scope | Done when |
|---|---|---|
| **1a: Quality guard** | Q1 (done), Q2, Q3, Q4, Q5. | `edit compare` between v1 and any render prints no clip under 85% sharpness; the report lists every source's resolution. |
| **1b: Safe, dynamic framing - DONE 2026-10-24, see 0.5** | S1-S5 on the YouTube and Kick layouts. | `verify` "overlay clipped" passes on Mish Mash #1 re-rendered; no punch crops a cam; scenes map matches a hand check on 20 sampled frames. |
| **1c: Moment engine v1 - BUILT 2026-10-24, see 0.6** | M1-M6; rebuild the Mish Mash lineup from evidence sheets, with kill context. | Every kill chapter clip has lead-up and aftermath; coverage report per game; you review a candidate sheet, not a list of descriptions. |
| 2 | Ears: A1 word timestamps, A2 reaction map (also feeds M3), B1 captions, H checks. | as before |
| 3 | Review loop (E). | as before |
| 4 | Look (B3 graphics kit, B4 punctuation, B5 transitions, B2 reaction framing, on top of S). | as before |
| 5 | Sound (A3, C2, C3, C5). | as before |
| 6 | Finish and learn (B6, F upload, E5 analytics). | as before |

A2 (reaction map) moves up only as far as M3 needs it; the full "ears" work stays in phase 2.

### 0.5 Phase 1b result (2026-10-24)

What was built, against S1-S5, and what was not.

| | Status | What exists |
|---|---|---|
| S1 scene map | **partly** | `editscene.py` finds the webcam box (cyan-bordered rectangle) in every frame and gives per-second spans per clip, with frame-accurate layout changes and an `unstable` marker for a sliding box. Hand-checked on 54 frames from both platforms (47 boxes, 7 full-cam scenes). **Not built:** classifying menus, maps, black screens, full-cam (no box is "no webcam"), the chat panel or HUD rectangles, and a whole-stream `scenes.json`: the map is made per clip during a render. |
| S2 safe zoom | **done** | `editframe.safe_camera`: the window always holds the box (exact with the margin; 3 px at 1080p for a box flush with the source's edge), the zoom is capped only if the box would not fit; the pan changes between two frames at a layout change. Tested over 400 random boxes and through a real render. The webcam still zooms with the picture; the pivot moves a few percent towards it. |
| S3 targeted punch | **not built** | Focus is still the authored (0.5, 0.5) plus the safe shift. No crosshair or face locator, so a full-cam zoom still crops the chat panel and the logo. Belongs with B2 (reaction framing) in phase 4. |
| S4 verify | **done, stronger than planned** | `webcam in frame` reads frames of the *render* and runs the detector on them (not a plan-level geometry check), plus a contact sheet with the expected box drawn in magenta. A deliberately wrong camera fails it. |
| S5 lower-thirds | **done** | `clear_of_webcam` moves an overlay that would sit on the box (sideways, else up or down), over the whole span the box occupies while the overlay is up; a warning says what moved. |

Also found and fixed on the way: the corner-pivot bug above, and a detector false positive on a
test pattern's cyan areas (filled areas and instants are now rejected).

**Done-when check, measured on the Mish Mash re-render (v3, 47 zoomed clips):** `webcam in frame` passes on
129 of 129 sampled frames. The same check run on the v2 render against the same expectations matches 30 of
129 and finds a mismatch in 44 of 47 clips, the box displaced by a median 3.3% of the frame width (the
corner pivot). The detector had to be loosened twice to read boxes back out of a render (see CLAUDE.md,
editscene); the first run of the check on v3 reported 5 misses that were the detector's, not the camera's.
**Caveat, said plainly:** no clip in the first episode changes layout mid-clip, so the mid-clip switch path is
proven on a synthetic video only.

### 0.6 Phase 1c result (2026-10-24)

| | Status | What exists |
|---|---|---|
| M1 scan every stream | **partly** | The kill reader now also reads the `KILL CONFIRMED` popup at the crosshair (no stream layout covers it), joins it to the feed rows and reports `source: both / feed / confirm`. On the Kick stream of 15 September (5.9 h, webcam over the feed) the feed reader found **no** kills and the popup found **39**. Scans of 11 September, a short 15 September stream and the 25 September YouTube stream were started in the background (about 30 minutes of OCR per 3-6 hours of video); the five streams scanned before the popup pass existed still need a `--force` rescan, deliberately not done unasked because it rewrites their icon files, which older edit specs point at. |
| M2 arc builder | **built, with a measured limit** | `editmoment.arcs_from_kills`: kills within 16 s are one arc, 14 s lead-up and 7 s aftermath, touching arcs merged (or split halfway between two kills if over 90 s), edges moved into a pause. **Not a detection of "first gunfire":** on 70 known kills the rate of sharp high-frequency audio onsets in the 40 s before a kill was 0.34 per second, at random times 0.35 - the mix is the voice - so lead-up is a policy and the contact sheet corrects it. |
| M3 rank | **built, weights unvalidated** | `score_arc` with printed parts: kills (nade/headshot/distance), multi, loudness spike, chat burst, analysis pick. The weights are hand-set; the top of the list looked right on the sheets, but nothing has been fitted to what viewers liked. A kill known only from the popup has no weapon or distance, so its `kills` part is flat. |
| M4 evidence sheet | **built** | `clipbot moments sheet` (per arc, or `--range` for any stretch by hand): a dozen frames, kills marked, transcript lines, the analysis's words; `moments show` prints what the picture cannot (Devanagari). |
| M5 completeness | **not built** | The end-of-match scoreboard is not read. `moments coverage` is a verdict per stream ("not scanned", "feed only - rescan", "unread or no kills", "feed mostly covered", "ok"), not a count of the kills that happened. |
| M6 context rules | **built** | `edit episode` prints a `CONTEXT:` line for any kill clip with under 10 s before its first kill or under 5 s after its last (`"cutaway": true` exempts); lineup clips can be `{"moment": id, "trim": [..], "at": first_kill}` so a moment is chosen by id from a sheet. |

**What the review found** (a sheet of every one of the 55 clips with 6 s either side): a clip captioned "mortar
landed on my face" ran over a quiet walk through a yard - he was *retelling* it, so the footage had no mortar (cut); "I hope you
get him - and the kill comes" ended 0.4 s before its punchline, "This is my first Nade game" (extended); two Base
clips and one helicopter clip show a death screen or a wall while the voice carries the joke (kept, said so); a 25 s static
title screen carries a New Look line (kept, not fixed). The Receipts chapter was rebuilt from six arcs (21-37 s each, at
least 10 s before and 5 s after the kills, against 9-16 s with 6 s and 3 s) and went from 86 s to 160 s; the episode from
10:07 to 11:27.

**Still open:** the rescan of five older streams; a better answer for talking-over-nothing clips than "keep or cut" (reaction
framing on the webcam, B2, needs a face and a higher-resolution cam than the 510 px box in a 1080p frame); the ranking
weights; the scoreboard check; transcripts are still the only way to find a non-kill moment, and they point at *talk about*
the moment.

## 1. Where we are

What the engine does well today (measured on Mish Mash #1, 10:07, 1080p60):

- **Structure and timing.** 56 clips from 10 streams across Kick and YouTube, cold open,
  sting, 11 YouTube-eligible chapters, cuts snapped into speech pauses
  (36 clips moved), clip levels matched across sources.
- **Technical delivery.** -14.2 LUFS, -1.5 dBTP, A/V within 18 ms, no black,
  frozen or silent stretches, no clicking joins, pacing gate passed.
  The final rendered in 745 s; a re-render reuses every cached clip (52 s).
- **Honesty gates.** Kill callouts come from the kill-feed reader, and the 238 m and
  99 m kills were confirmed in frames. A wrong candidate (the "23-kill game" that was
  a DEFEAT screen) was caught by pulling a frame.

What the audit found keeping the edit from **looking and sounding high-production**,
ordered by how much a viewer would notice:

| # | Gap | Evidence from Mish Mash #1 |
|---|-----|----------------------------|
| 1 | **I can't hear.** Moments are picked and trimmed from noisy Hindi transcripts, chat counts and frames. | Whisper garbled lines ("main nahi chudi" in the cold open, unverified swear). Joke timing is a guess within ~0.3 s. |
| 2 | **No captions.** A Hinglish-speaking channel with jokes that land on the words has no on-screen text except chapter labels. | `captions.json` exists for only 4 streams; nothing uses it in edits. |
| 3 | **Static or dead picture.** The story is in the audio while the picture is a loading screen, a privacy-policy page, a black screen or a minimap. | c3_19/c3_20 (Wardogs title screen), c5_31 (privacy policy), c8_49 (black screen + small cam). |
| 4 | **The face is small.** Reactions sell the comedy, but the webcam is 25% of a 720p frame (320x180 source pixels). | Every Kick clip. Rule 13 forbids zooming into the cam because there are no pixels to zoom into. |
| 5 | **Plain graphics.** One default font, a flat plate lower-third and a text-only sting. No brand kit, no animated intro, no designed end screen. | Sting card = black + white text; end card = black + text. |
| 6 | **Thin sound design.** 3 music beds, 9 SFX, no voice processing, game audio never ducked under speech. Teo's series uses Epidemic Sound music, credited in his descriptions. | `clipbot library list`: 4 fonts, 3 music, 9 sfx, 10 stickers. |
| 7 | **No editorial punctuation.** No freeze-frame-and-zoom, replay, meme sound or caption pop on punchlines; the only "edit" moves are a slow push-in and a punch zoom. | `_ambient_camera` + punch keys only. |
| 8 | **Mixed source quality.** 54% of the runtime is Kick 720p60 upscaled with Lanczos + unsharp, cut against native 1080p60 YouTube. | `post.md` note. |
| 9 | **Thumbnail is a frame, not a design.** Auto-picked sharp frames; nobody designed text, face or contrast. | `deliver/thumbnails/thumb_1..3.jpg`, plus the two face-forward picks I added. |
| 10 | **No review loop with you.** You see a finished render; there is no place to drop "cut this", "louder here" at a timestamp, and no learning from how the video performs. | The workflow ends at `edit deliver`. |
| 11 | **QC can't see taste.** Verify passes technical checks only. It missed the dead-picture clips, can't judge caption legibility, and doesn't catch level jumps at joins or a swear in the first 30 s. | All 11 checks passed on a cut with the issues above. |

---

## 2. What "complete editor" means here

One loop, each step owned by a tool so it can be repeated and improved:

```
 PERCEIVE ──> SELECT ──> ROUGH CUT ──> YOUR NOTES ──> FINE CUT ──> FINISH ──> QC ──> DELIVER ──> LEARN
 (hear+see)  (evidence)   (preview)    (timestamped)  (polish)   (gfx/sound) (v2)  (post+thumb) (analytics)
     ^                                                                                              |
     └────────────────────── rules, taste and the library get better every video ───────────────────┘
```

The bar is a **production checklist** that verify v2 can score.
- Every spoken punchline is captioned.
- The face is visible at a readable size when the reaction is the point.
- No dead picture lasts more than 2 s.
- Voice sits clearly over game audio.
- Music is edited to the cut.
- The graphics share one brand kit.
- The thumbnail is designed.
- Levels never jump more than 3 LU between neighbouring clips.

---

## 3. Workstreams

Each workstream lists why it's needed, what to build, the tools involved, its cost or risk, and when it counts as done.

### A. Perception: give the editor ears and better eyes

| Item | What | Tools (checked on this PC) | Done when |
|---|---|---|---|
| A1 Word-level timestamps | Re-transcribe selected windows with word timing, so cuts land between words and captions can be timed per word. | `faster-whisper 1.2.1` is installed (`word_timestamps=True`, runs on CPU). OpenAI whisper-1 also returns word timestamps. WhisperX adds wav2vec2 forced alignment, but [its Hindi alignment is unverified](https://arxiv.org/html/2303.00747v2). | `editcut` snaps to word boundaries; a test asserts no cut inside a word. |
| A2 Reaction map | Tag every second of a stream with audio events (laughter, shouting, gunfire, music, silence), so "he loses it laughing" becomes a measurement instead of a transcript guess. | [YAMNet](https://www.tensorflow.org/tutorials/audio/transfer_learning_audio) has Laughter / Giggle / Belly laugh classes among its 521 AudioSet classes. PANNs (PyTorch) covers the same ontology and fits the installed `torch 2.8` better. | `work/<slug>/events.json`. Lineup picks cite a laugh peak. The analyze prompt gets the tags like the energy spikes it gets now. |
| A3 Voice/game separation | Split each clip into a voice stem and a "rest" stem. That allows ducking game audio under speech, cleaning the voice, and removing copyrighted music. | [Demucs htdemucs](https://cdn.jsdelivr.net/gh/facebookresearch/demucs@main/README.md) is trained on music, so a speech-over-game-audio test is needed. On CPU it is about 12x slower than GPU, so run it per clip, not per stream. | A measured voice-to-rest ratio per clip, and the mix rides the rest stem under speech. |
| A4 Picture state | Classify frames as gameplay, menu/loading/text screen, black, or full-cam scene. | PySceneDetect for cuts, plus a light classifier (Claude contact-sheet pass or a small CNN). | The lineup warns "dead picture" before render; verify v2 fails a menu shot over 2 s. |
| A5 Cam locator | Find the webcam box per stream layout (Kick: left-middle; YouTube: top-right) and track the face. | OpenCV face detection on 1 fps frames. | `layout.json` per workspace, used by B2. |
| A6 Optional second opinion | An audio-capable model listens to a clip and returns timestamped events and emotion. | [Gemini video/audio understanding](https://ai.google.dev/gemini-api/docs/audio) samples 1 fps video plus audio with timestamps. The docs say speech inference is English-only, so it is useful for non-speech events, not Hindi jokes. | Off by default; your API key and cost. |

### B. Engine: picture

| Item | What | How |
|---|---|---|
| B1 **Animated captions** | Word-pop captions (Hinglish by default, an English option), safe-area aware, styled from the brand kit, plus an `.srt` uploaded to YouTube for accessibility and search. | The vendored ffmpeg has **libass**, freetype and harfbuzz (checked). Generate `.ass` with karaoke tags from A1 words. |
| B2 **Reaction framing** | When the reaction is the point, switch layout. Options are a split screen with the game left and an enlarged cam right, a punch to the cam on a full-cam scene, or game plus a big cam corner. Rule 13 becomes "zoom into the cam only where the source has the pixels". | New `framing.layout` presets using A5 boxes. Full-cam scenes (the new-look reveal, the dance) qualify; the 320x180 Kick cam caps at about 2x. |
| B3 **Motion graphics kit** | Animated sting/intro, lower-thirds, chapter cards, end screen with subscribe/next-video slots, kill callouts with motion. All brand-consistent. | [Remotion](https://blog.openreplay.com/making-videos-claude-code-remotion/) (React to video; a Claude Code skill exists, `remotion-dev/skills`) renders alpha `.mov` (ProRes 4444/QTRLE) that the engine composites. Check Remotion's licence (one source says companies of 4+ need a licence; an individual is likely free). Needs Node 22+; Node 20.19 is installed. |
| B4 **Punctuation toolkit** | Freeze-frame plus zoom plus a sound hit on a punchline, instant replay at 0.5x, impact frames, meme-sound pops, shake on explosions, a "record scratch" stop. Each is a named spec macro the lineup can call (`"beat": "freeze_zoom"`). | Mostly existing primitives (freezes, camera keys, speed keys, fx, SFX) wrapped as macros, plus a few new filters. |
| B5 **Transitions with intent** | Whip-pan blur, zoom-through, light-leak and glitch for chapter changes, and hard cuts inside chapters. | xfade custom expressions plus overlay assets (light leaks, film grain) from D. |
| B6 **Look** | Match Kick and YouTube colour per source, an optional channel LUT, and better upscaling of 720p. | `lut3d` and `zscale` are in the build. **realesrgan-ncnn-vulkan runs on AMD through Vulkan**, so the RX 7800 XT can AI-upscale the Kick footage. Measure the gain with `libvmaf` (in the build). |
| B7 ~~Faster iteration~~ **dropped after measuring** | Hardware-encoded previews. | `h264_amf` took 2.3 s against x264 ultrafast's 1.4 s for 20 s of 1080p60 and made a bigger file at the same quality; the preview time is segment fetching and filtering (74 s cached, 328 s cold for a 10-minute episode), not encoding. |

### C. Engine: sound

| Item | What | How |
|---|---|---|
| C1 Voice chain | Denoise, de-ess, compress, add presence; dialogue-anchored loudness. | `arnndn`, `afftdn`, `deesser`, `acompressor`, `dialoguenhance`, `speechnorm` are all in the build (checked). Applied to the A3 voice stem when separation works, else to the full mix with gentler settings. |
| C2 Game ducking | Game audio dips 4-8 dB under speech, not just music under game. | Sidechain on the voice stem, or on a speech-activity envelope from A1/A2. |
| C3 Music editing | Beds cut to length and to the beat; hits land on cuts; stingers. | `editaudio.detect_beats` already exists. The [Epidemic Sound MCP](https://www.epidemicsound.com/blog/mcp-server/) can cut a track to a length and loop it. |
| C4 Join smoothing | Level jumps between neighbouring clips checked and smoothed with 80-150 ms crossfades on talk-to-talk cuts. | Per-join LUFS delta in verify v2; `acrossfade` in the assembly graph. |
| C5 Content-ID safety | Flag music inside clips (A2 music tag) and offer to replace it from the rest stem, or mute it. | A3 + A2. |

### D. Editing resources library

The current library is 4 fonts, 3 music beds, 9 SFX and 10 stickers. Target: a **brand kit plus curated packs**, each asset with its licence recorded (the `library licenses` command exists).

- **Music:** [Epidemic Sound](https://developers.epidemicsound.com/docs/mcp/) is Teo's source. Search is free; downloads need a paid subscription; it offers an MCP in beta with OAuth or API key. Free alternatives: YouTube Audio Library (manual download), Pixabay (manual).
- **SFX:** Epidemic (250k SFX), plus [Freesound](https://github.com/ilesinge/freesound-api). Originals need OAuth2; filter on the licence field, not assumptions. Build packs: whooshes, risers, impacts, glitches, record scratch, UI pops, crowd and meme reactions.
- **Fonts:** Google Fonts (OFL) for a two-font brand pair, one display and one caption. Hindi glyphs (Devanagari) for an occasional on-screen line.
- **Overlays/LUTs:** light leaks, film grain, dust, a channel LUT, and an end-screen template.
- **Brand kit file:** `library/brand.json` holding colours, fonts, plate styles, the sting/end-card Remotion compositions, the caption style and the thumbnail template. Every template reads it, so one change restyles every future edit.

### E. Review and iteration with you

1. **Edit review page.** A private Artifact page (or a dashboard route) per edit. It has the preview player, the chapter list, each clip with its evidence (transcript line, frame, reaction peak), and **timestamped comments**. I read the comments and turn them into spec changes, then re-render only the changed clips. You can do this from your phone over Remote Control.
2. **Versioned cuts with a changelog.** `edit render` writes `v1, v2, ...` with "what changed" (clips added/cut/trimmed, gains, graphics).
3. **A/B options where it matters.** Two cold opens, two title cards, three designed thumbnails.
4. **Taste file that actually grows.** Every note that's a preference ("no whoosh", "captions bigger") becomes `edit taste add`, and templates read it.
5. **Post-publish learning.** Use the YouTube Analytics audience-retention report (`audienceWatchRatio` over `elapsedVideoTimeRatio`, plus `relativeRetentionPerformance`) to map dips to chapters and clips, and record the findings as rules. This needs an extra read-only Analytics OAuth scope; today's token is upload-only.

### F. Finishing, hand-off and publishing

- **Pro-NLE hand-off (optional).** Export the edit as OTIO/FCPXML so you or a Resolve MCP can finish in DaVinci Resolve. Caveats from the research:
  - Exported effects don't translate between apps.
  - Resolve's API import has linking issues.
  - The [community Resolve MCPs](https://github.com/lordhoell/davinci-resolve-mcp) mostly need **Resolve Studio** for scripting.

  This is a path, not the default.
- **Designed thumbnails.** A compositor (Pillow or Remotion) takes the best face frame from the A5 face track plus an expression score, cuts out the subject, adds 2-4 words of bold text and the brand frame, and checks legibility at 120 px.
- **Upload the edit.** Extend `clipbot upload` to upload an edit. It goes up **private** with the chosen title, description, chapters, `.srt` and thumbnail, and only after you confirm. The resumable client is already generic.

### G. Skills and MCPs to add

| Add | Why | Needs from you |
|---|---|---|
| Epidemic Sound MCP | A pro music and SFX catalogue with edit-to-length, licensed for YouTube. | A subscription, for downloads. |
| Remotion skill (`remotion-dev/skills`) | Motion graphics written as code, rendered on this PC. | Node 22+ install; a licence check. |
| DaVinci Resolve MCP | Optional finishing in a pro NLE. | Resolve Studio (paid). |
| `clipbot-edit` skill v2 | Format playbooks (Mish Mash, kill montage, Shorts), the production checklist, punctuation macros, a caption style guide, and how to read A2/A3 evidence. | Nothing. |
| `clipbot-review` skill | Turns review comments into spec edits and taste rules, and writes the changelog. | Nothing. |

### H. QC v2: verify what a viewer notices

Add these checks to `edit verify`:
- dead picture over 2 s (A4)
- face visible when a clip is tagged reaction (A5)
- caption legibility and safe areas (B1)
- level jump at a join over 3 LU (C4)
- voice-to-rest ratio under N dB (A3)
- profanity in the first 30 s, from word timestamps (A1)
- music inside clips (C5)
- encode quality against the segment cache with VMAF (in the build)
- thumbnail legibility at 120 px

Each check comes with a contact-sheet artefact, as verify does today.

---

## 4. Phased roadmap

> **Superseded by the re-ordered roadmap in 0.4**: phase 0 is done, and the next phase is fixing the three problems from the v2 feedback (quality guard, safe dynamic framing, moment engine) before ears and captions. The table below is the original ordering, kept for the scope of each item.

| Phase | Scope | Why first | Size |
|---|---|---|---|
| **0: Quick wins** | B7 AMF previews; C1 voice chain on the mix; C4 join-level check and crossfades; A4 dead-picture detector (a contact sheet plus my review); D brand kit file plus fonts; F designed thumbnail v1; re-render Mish Mash #1 as v2. | Biggest visible and audible lift for little new machinery. Proves the loop on a video you've already seen. | 1-2 sessions |
| **1: Ears** | A1 word timestamps; A2 reaction map; B1 animated captions; H profanity and caption checks. | Captions and word-accurate cuts are the largest jump in watchability, and the reaction map closes my "can't hear" gap. | 2 sessions |
| **2: Review loop** | E1 review page with comments, E2 versions and changelog, E4 taste wiring, the `clipbot-review` skill. | Makes every later improvement iterate with you, not around you. | 1-2 sessions |
| **3: Look** | B3 Remotion graphics kit; B4 punctuation macros; B5 transitions; B2 reaction framing with A5. | High-production feel; needs Phase 1 timing to place beats precisely. | 2-3 sessions |
| **4: Sound** | A3 separation test; C2 game ducking; C3 music editing with Epidemic (if subscribed); C5 Content-ID safety. | Depends on the A3 measurement and the music source decision. | 2 sessions |
| **5: Finish and learn** | B6 AI upscale (Vulkan) with VMAF check; F upload of edits; E5 analytics loop; optional OTIO / Resolve. | Polishing the source and closing the performance loop. | 2+ sessions |

Every phase ends with the same proof:
- tests pass
- docs and the skill are updated
- Mish Mash #1 is re-rendered with the new capability
- a side-by-side frame and sound report, so you can judge the difference

---

## 5. Decisions only you can make

1. **Music source:** Epidemic Sound subscription (what Teo uses, with an MCP), or free libraries only?
2. **Captions:** Hinglish (Latin script) on screen by default? An English subtitle track for YouTube as well?
3. **Branding:** do you have channel colours, fonts or a logo to build the kit around, or should I propose two directions?
4. **Optional paid or external services:** a Gemini API key for audio second opinions (A6)? Resolve Studio for NLE finishing?
5. **YouTube Analytics scope:** OK to add a read-only analytics permission to the existing upload login, for the learning loop?
6. **Mish Mash #1:** keep it as the test bed and re-cut it at the end of each phase, or publish v1 now and use #2 for the next phase?

## 6. Known limits (said up front)

- PyTorch on this PC runs on CPU: there's no ROCm on Windows. GPU acceleration comes through Vulkan (Real-ESRGAN ncnn, whisper.cpp) and AMF encoders, not torch.
- Separation, laughter and word alignment are **unmeasured on Hinglish speech over game audio**. Each one gets a small measured trial before it is trusted, the same way the kill-feed reader did.
- The 320x180 Kick webcam can't become a sharp close-up. Reaction framing helps most on YouTube and full-cam scenes.
- I'll still not hear the final the way you do. The loop is built so your notes, the reaction map and the analytics stand in for that, and every gap it can't close is reported, not hidden.
