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
| **1a: Quality guard - only Q1 built** | Q1 (done), Q2, Q3, Q4, Q5 (Q2-Q5 not built as of 2026-10-27: there is no `edit compare`). | `edit compare` between v1 and any render prints no clip under 85% sharpness; the report lists every source's resolution. |
| **1b: Safe, dynamic framing - DONE 2026-10-24, see 0.5** | S1-S5 on the YouTube and Kick layouts. | `verify` "overlay clipped" passes on Mish Mash #1 re-rendered; no punch crops a cam; scenes map matches a hand check on 20 sampled frames. |
| **1c: Moment engine v1 - BUILT 2026-10-24, see 0.6** | M1-M6; rebuild the Mish Mash lineup from evidence sheets, with kill context. | Every kill chapter clip has lead-up and aftermath; coverage report per game; you review a candidate sheet, not a list of descriptions. |
| 2 | Ears: A1 word timestamps, A2 reaction map (also feeds M3), B1 captions, H checks. | as before |
| 3: Review loop - BUILT 2026-10-25, see 0.9 | E1 review page with timestamped comments, E2 versions and changelog, E4 taste wiring, the `clipbot-review` skill. | as before |
| 4: Look - BUILT 2026-10-26/27, see 0.10 and 0.11 | B4 punctuation, B5 transitions and B3 the graphics kit (Remotion motion graphics, the HUD look) built; B2 reaction framing not. | as before |
| **R1: Reels, vertical layout - PLANNED 2026-10-27, see 0.12** | Stacked game+cam and full-cam layouts, vertical captions, a `reel` template and `clipbot edit reel` for one clip. | Three approved clips (Kick talk, Kick game, YouTube) render as 1080x1920 edits that pass `verify`. |
| **R2: Reels, batch and delivery - PLANNED** | One command per workspace's approved clips, cover frame and post text per reel, a reels queue page. | A workspace's approved clips become a folder of finished reels with post text. |
| **R3: Reels, hook and polish - PLANNED, optional** | A hook line, 9:16 versions of the graphics, sound. | A batch you have seen and would keep. |
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

### 0.7 Phase 2 result (2026-10-24)

| | Status | What exists |
|---|---|---|
| A1 word timestamps | **done** | `editwords.py`: whisper-1 on only the windows an edit uses (14.7 min of audio for the 53 clips of the first episode, $0.09, cached per workspace), then **corrected against the sound** - collapsed words (several given one instant) are spread, and each word gets the stretches where the signal is actually up, because whisper holds a short word over a pause (a three-letter word for 1.4 s). A cut is "inside a word" only where the sound is up. On the first episode 17 of 106 cut points split a word; `edit episode --words` moved what could be moved (a clip only grows, <= 0.6 s) and 6 remain (pinned clips, or no clean place within reach). |
| B1 captions | **done, with a measured limit** | `editcaptions.py`: the model writes Hinglish chunks over the numbered words and rates each (`conf`); cues come out timed from the words they cover, drawn with libass karaoke (the word being said lights up), brand font, in a box left of centre so the chat panel (bottom right) and minimap (bottom left) stay clear. `captions.json` is yours to edit. Cues under `captions.min_confidence` are kept but not drawn. Clips with kill callouts get none unless asked. |
| H checks | **done** | `verify` gains **cuts inside words**, **strong language** (first 30 s of the output) and **captions on screen** (reads the render). `edit captions` prints strong language with its timestamp. On the first episode the cold open says "bastard" at 0:11, which is a decision for you. |
| A2 reaction map | **built after you said yes (2026-10-24); measured, and weaker than hoped** | `clipbot events`: the PANNs `Cnn14_16k` tagger over `audio.wav`, per second, ~95x realtime. Gunfire checks out against the kill record; laughter is a weak hint and shouting is barely detected, so the reaction map is **off** in ranking and in analyze. Section 0.8. |

**The finding that changed the work: the language pin.** The first captions came out 48% unsure (176 of 365 cues) and
only 9 of 53 clips were fully captioned. The cause was not the caption model: word timing had been fetched with
`language=hi`, the pin the stream's own transcript uses. A Hinglish streamer switches to English in mid-sentence, and
on those windows Hindi-forced returned Devanagari nonsense (`इ कि जो हुड़ा है भे मुझे पинки मर`) where auto-detect
returned `wait bro wait, stop doing that bro`; over loud game audio it returned "Like share and subscribe". Ten real
windows, three modes: Hindi-forced, auto, and auto with a Roman-script Hinglish prompt. The prompted mode was right or
better on 8, equal on 2, and wrote the Hindi as people type it (`mere pe mortar gir gaya, bro bro, help!`). Switched
(`words.language` null, `words.prompt`), windows refetched ($0.09): **23% of cues unsure (96 of 410), 14 clips fully
captioned, 2 with none.** What is left unsure is mostly rap lyrics and speech under gunfire.
**One cost:** a quiet window of real speech (c7_44, "I got one, I got two") came back as emoji where Hindi-forced had
found the words; symbol-only tokens are dropped and `edit words` names such clips. `gpt-4o-transcribe` was also tried
and is not usable: it returns a fragment of the speech and no word times.

**A false failure found on the way, and what it was.** The first preview verify of the captioned episode said
`webcam in frame` FAILED for two clips, and the webcam was on screen in both. I suspected the burned-in captions;
an A/B on the same 224 sampled instants of the same clips rendered with and without captions found the box in
190 vs 192: not the captions. It was the detector on the 540p `ultrafast` preview: the webcam's border is a
one-pixel line and a low-quality encode smears it on some frames (14% of single frames), and two short clips
had a single sample each. A sample is now judged on the frames of its 0.15 s (each against where the camera is at
that frame); a box in the wrong place on every frame is still a miss. Misses fell to 5% and the verify passes.
Final-tier renders had passed 129 of 129 before, so preview misses remain warnings.

**Not fixed, and said plainly:** the project's own stage-3 transcript is still Hindi-pinned. It feeds clip finding
(`analyze`), so the same English-as-nonsense problem is in what picks the moments. Re-transcribing a stream costs
about $1.66; I have not done it. It is a decision for you (see section 5). Nothing here can hear a clip, so a caption
the model rates as sure can still be wrong - read `captions.json`.

### 0.8 Phase 2b result: the reaction map (A2), 2026-10-24

You said yes to fetching the checkpoint, so I built it and then measured it before letting it touch anything.

**What was downloaded, so there is a record.** `Cnn14_16k_mAP=0.438.pth` from [Zenodo record 3987831](https://zenodo.org/records/3987831)
(the PANNs authors' own release, CC BY 4.0), 358,668,570 bytes, MD5 `362fc5ff18f1d6ad2f6d464b45893f2c` - identical to the
checksum the record lists. I chose the 16 kHz variant over the 327 MB 32 kHz one I first mentioned: `audio.wav` is 16 kHz, so no
resampling, and it scores a little higher. It sits in `work/_cache/models/panns/` (gitignored), alone in its own folder.
The file is torch's old pickle format and also carries one numpy array (937,432 training-sampler indices), which the safe loader
refuses by default; I disassembled the pickle (nothing executed) and allow-listed exactly the six globals it names, and nothing else.
The architecture is written out in `panns.py` rather than installed from pip, so no third-party code runs - only third-party weights.

| | Result |
|---|---|
| Speed | 90-100x realtime on this CPU: a 7 h stream in 4.4 minutes, 5.5 h in 3.4. |
| Sanity on synthetic audio | quiet noise -> Silence, a 440 Hz tone -> Sine wave, loud white noise -> White noise. |
| Dense per-second method vs real per-window inference | the category scores differ by a median 0.000 (90th percentile 0.024) on 24 random seconds of real audio. |
| **Gunfire vs the kill record** (19 kills) | AUC 0.74; 58% of kills score >= 0.25, against 20% of random moments (shooting is everywhere in this game). Usable as "there is shooting here", not as "a kill happened". Explosion: AUC 0.61, and 0% of kills. |
| **Laughter vs the chat's laugh emotes** (five Kick streams, 28-38 messages each) | AUC **0.49, 0.61, 0.73, 0.61, 0.64** (mean 0.62; 0.50 is chance). Plain loudness on the same moments: 0.44, 0.61, 0.63, 0.46, 0.58 (mean 0.54). |
| **Top peaks, read by hand** | Of the 86 strongest laughter peaks (20 per stream), chat laughed within 12 s after 6 (7%); a random moment: 3%; the 20 loudest seconds: 2%. The top 12 on the Valorant stream were game callouts ("spike hai", "win win win", "clear") with no chat reaction. |
| **Shouting** | 0-4 peaks per stream at >= 0.25: the model hardly ever calls this streamer's excitement a shout. |

**Verdict.** The plumbing is sound and fast. Gunfire is real. Laughter carries a weak signal - consistently above chance and above
loudness in four of five streams - but nowhere near enough to find funny moments: about nine in ten of its best peaks were not
followed by chat laughing. The label I measured against is itself poor (these chats laugh about 30 times in four hours, and also at
chat), so the test has little power and I would not call the answer final. What I would not do is let a number that weak reorder
your moments. So `moments.voice_weight` (0) and `analyze.signals.events` (false) are **off**; the map is recorded, shown in
`moments show` and `events peaks`, and one setting turns it on. **What would settle it:** ten to twenty moments you *know* you
laughed or shouted in (decision 8 in section 5). `clipbot events check --workspace W` re-runs this whole measurement on any stream.

### 0.9 Phase 3 result: the review loop (E), 2026-10-25

| | Status | What exists |
|---|---|---|
| E1 review page | **built, as a dashboard page, not a hosted artifact** | `/edits/<name>/review`: the render playing (preview, the live final, or any archived version), a timeline with chapter ticks and your comment markers, "what is on screen now" under the video (clip, source and range, chapter, the caption it shows), tabs for comments / chapters / clips / versions / taste, and keys for it: **C** pauses and opens the comment box, **Ctrl+Enter** adds, space and the arrows move. A comment records the clip and the offset into it, not only the second, so it survives a re-render. Comments live in `work/_edits/<name>/review.json`, the same file `clipbot edit review` and I read and write; the page picks up what the CLI writes within 8 s. Checked in the browser against the real Mish Mash edit: the video loads at 11:27, a comment added from the keyboard lands on `c6_39` in the right chapter, switching to v4 and the live final plays. |
| E2 versions and changelog | **built** | A final render now archives the final before it as `render/v<N>/` (with its spec, timeline, lineup and captions), writes `versions.json` and `changelog.md`, and prints what changed in words: `c3_14: in 405.55 -> 405.2 s, clip 0.35 s longer`, `gain 0 -> -3 dB`, `added c9 (...)`. A failed render leaves the live final where it was. Previews are not versioned. Mish Mash #1's hand-made v1, v2, v4 are recognised; its live final is v5. |
| E4 taste wiring | **partly** | A comment of kind `taste` turns into a standing rule with your words kept (`edit review NAME taste ID`, or the page's "-> taste rule"); `edit new` and `edit episode` print the rules in force. **Not built:** templates applying a rule by themselves - nothing in the renderer reads taste, by design (a taste is a judgement and belongs in the spec where you can see it), so the author applies it. |
| `clipbot-review` skill | **built** | The procedure for turning comments into changes: follow the clip and offset, make the smallest change, re-render, *look*, then resolve with what was changed and where it was seen; ask when a note is ambiguous; never resolve without looking. |
| E3 A/B options, E5 retention loop | **not built** | E3 needs a second variant of a cold open or thumbnail to compare; E5 needs the extra read-only YouTube Analytics permission (decision 5 in section 5). |

**Why a dashboard page and not an artifact.** The plan allowed either. The preview alone is 314 MB and a hosted page carries 15 MB
per file, so the video cannot live there; the dashboard already serves video with seeking. The cost: it is loopback-only by
design (no auth, per CLAUDE.md), so it works from this PC, not from your phone. If you want phone review, the honest options
are a short low-bitrate proxy render on a hosted page (watch there, comment there, I read the comments through the page's
shared state) or opening the dashboard beyond loopback, which needs real authentication first. Decision 9 in section 5.

**Found on the way:** `cli.main` did not catch `SpecError`, so a mistake in any edit command (an unknown taste id, a bad time)
printed a Python traceback. It now prints the message and exits 1.

### 0.10 Phase 4 result: the look (B4, B5), 2026-10-26

| | Status | What exists |
|---|---|---|
| B4 punctuation | **built** | Named moves a lineup clip can call, `"beats": [{"do": ..., "at": source second}]`: **`freeze_zoom`** (hold the frame for `hold` s, punch in, optional flash, sound and a caption), **`replay`** (the moment again after it at 0.5x, tagged REPLAY, a flash in), **`impact`** (shake, flash, sound, optional sticker), **`caption_pop`** (a big caption with an optional sticker). `clipbot edit beats` lists them. **Not built:** record scratch (the library has no such sound) and meme-sound pops. |
| B5 transitions | **built** | A chapter's `"transition"`: **`whip`**, **`glitch`**, **`zoom_through`**, plus `flash`, `dip`, `crossfade`; a preset brings its own sound and replaces the chapter whoosh. **Not built:** light leaks and film grain (need overlay assets, section D). |
| B3 graphics kit | **not built** | Remotion needs Node 22+; this PC has Node 20.19, so it means installing Node and a package tree. Decision 10. |
| B2 reaction framing | **not built** | It needs a face or crosshair locator (A5 / S3, never built) and, on Kick, the webcam is 320x180 source pixels, so an enlarged cam is soft. |

**Transitions were measured, not named from the docs.** I rendered 16 xfade candidates between two real 1080p clips and looked at
the middle frame of each. `hblur` is a genuine whip-pan streak (heavy at 0.5 s, right at 0.3 s); `pixelize` is a convincing digital
glitch; `zoomin`, the obvious "zoom-through", goes through a muddy brown-black at its midpoint and is not used. So `zoom_through` is
a camera push on both clips under a plain cross-fade (and falls back to a cross-fade, saying so, when a punch is in the way or
a card is on either side).

**Rendered on real footage, then verified.** A 43-second demo edit, `look-demo` (four chapters, every beat and every preset, cut from
the same stream footage as Mish Mash), is in `work/_edits/look-demo/`; open it on the review page, `/edits/look-demo/review`.
Final-quality `verify` is OK: sound and picture agree to 0 ms, -13.9 LUFS, no click at any join, no level step, and the webcam is
whole in 12 of 12 sampled frames (the preview tier flags 4 of 12, and in each the box is plainly whole: the one-pixel border
smears at 540p).

**Things that went wrong on the way, and are fixed.**
- My first `freeze_zoom` zoomed with an fx `punch`, which scales around the centre of the frame and would crop a corner webcam
  (HARD-RULES 38). Caught by reading it against the rule, before it shipped: the zoom is now a camera key that safe framing protects
  (rule 44, with a test that no beat emits a `punch`).
- A constant `"speed": 0.5` normalised to a key with `at: null` that `edit render` then refused to load from `spec.json`; nobody had
  used a constant speed before. Fixed in `editspec`, pinned by a round-trip test.
- The spec accepted a transition name, `fadebright`, that ffmpeg 8 does not have, and lacked `hblur`; the list is now exactly
  ffmpeg's, checked by a test.
- A 7-second replay tripped the pacing gate (nothing new on screen); it now has camera keys across it.
- The library's "Glitch" sound is 30 ms and the engine drops anything under 50 ms; the lineup maps `glitch` to "Error" instead.
- My zoom-through code kept a punch's zoom and silently dropped its ease-back; its test caught it.

**Restraint is the author's call.** Nothing here decides *when* a move belongs. Teo's roundups cut hard with a whoosh per chapter;
I have not added beats to Mish Mash #1. `rules/look.md` in the edit skill says when each earns its place.

### 0.11 Phase 4, second half: motion graphics (B3) and a real run, 2026-10-27

**Decision 10 answered yes, decision 11 answered with a bigger ask**: "go for it with node... create a brand new video with a new
style and name not as a demo but as a real run... full control". So: the graphics kit, then a video made with it.

| | Status | What exists |
|---|---|---|
| B3 graphics kit | **built** | Remotion (React) compositions drawn to ProRes 4444 alpha video by `graphics/render.mjs` and composited as a `motion` overlay (`editmotion.py`, "Motion graphics" in CLAUDE.md). `Title`, `OpTitle`, `Contact`, `Stats`, `Acquire`. A graphic is told its length and hit time and the spec says where it *lands*. |
| The HUD look | **built** | `lineup.look = "hud"` (`edithud.py`): a series card, an operation header per chapter, a contact plate on every kill (range counting up to a lock on a 0-300 m ruler), a debrief card; every number is the feed's or counted from the kills in the edit. |
| Sound for it | **built** | `hudsounds.py`: seven synthesised cues (CC0 by construction, numpy only), each peaking where its graphic lands; `clipbot library synth`. |
| B2 reaction framing | not built | unchanged (needs a face locator; Kick cam is 320x180). |

**Toolchain, measured.** Node 22.23.4 portable (35.6 MB zip from nodejs.org, SHA-256 checked against SHASUMS256) under `tools/`, not on PATH
(this PC's Node is 20.19). `npm install` of Remotion 4.0.539 + React 19.3.1: 254 packages, 220 MB, 30 s. **No browser was downloaded**: Chrome
142 on this machine has no old headless mode ("Old Headless mode has been removed from the Chrome binary"), but Remotion's
`chromeMode: 'chrome-for-testing'` drives it in new headless. A 1 s transparent 1080p60 ProRes 4444 clip is 9.6 MB and takes ~9 s to draw
(mostly browser start); the 21 graphics of the first video took ~4 min at half scale. ffmpeg 8 reads the alpha (`yuva444p12le`). Remotion refuses a
scale that does not give whole pixels (1/6 of 1080 = 180.036), so graphics are drawn at 1, 1/2 or 1/4 and ffmpeg scales the rest.

**The video: AFTER ACTION #1** (`work/_edits/after-action-1/`). A Wardogs mission debrief from three native-1080p60 YouTube streams (Sep 24, 26, 30):
a cold open that tests a range finder on the 238 m scope and cuts before the shot, the series card, four operations (BREACH: a five-kill room clear
and a nade-then-headshot double; OVERWATCH: 68 / 63 m and a nade; ZERO IN: 57, 73 and a 99 m headshot; THE SHOT: 238 m, slow-motion, the
large plate) and a debrief card. Moments were chosen from evidence sheets (`moments sheet`), not descriptions: three candidate arcs were dropped
because the sheet showed him being downed at the moment of the kill, or no target in frame. Facts are from the kill feed and counted from what is
in the edit: 14 kills, 3 headshots, 2 grenades, longest 238 m, mean range 51 m over the 14 that have a range, 3 streams.

**Result, measured.** 3:39.9, 1920x1080 at 60 fps, 513 MB (x264 `slow`), final render 915 s of which the 21 graphics (drawn at full scale) took
~8 min; the preview was 219.5 s / 106 MB in 430 s. `edit verify` on the final: **OK** - -14.1 LUFS, true peak -1.5 dBTP, A/V agree to 0 ms, no black,
frozen or static stretch, no click or level step at a join, pacing clean (something new at least every 4 s), no cut inside a word, the webcam whole in
all 30 sampled frames of the 10 zoomed clips; one **warning**: strong language in the first 30 s ("Get fucked" at 0:29; two more later: rule 42,
the author's call). The preview's one webcam failure (c3_08, "found None") was smear at 540p and did not recur at final. Each graphic's sound peak
was measured in the preview's mixed audio (not re-measured on the final): the title boom 80 ms before the card's slam, a contact lock 40 ms before its plate's hit (the engine places a peak
`lead` = 50 ms early on purpose). **Not checked:** the music bed and the synthesised cues were never *listened to* (there is no ear here: levels,
spectrograms and peak times only); eleven of the 14 distances were not checked by eye against the feed.

**Things that went wrong on the way, and are fixed** (each is in HARD-RULES 46-47 or a test):
- Remotion merged a demo `defaultProps` under real props: **every plate in the first preview said KHANNY**, and a kill with no range would have been
  drawn as 68 m. `verify` passed. Found by reading frames of the render; `Root.jsx` registers compositions with no demo props, pinned by a test.
- The first preview called a kill seven seconds after another a DOUBLE KILL, drew a ten-digit OCR "name", and repeated its own range as a headline.
- An "OK" stamp sat on the word COMPLETE and the tease's two header labels ran together: found in stills.
- A kill clip's last seconds were dead stretches under the 4 s gate (nothing after the plate); the aftermath now gets a slow push-in.
- A one-off script I wrote to patch `edittemplates.py` duplicated half of the file (`str.index` matched an earlier line); restored from a copy I had
  taken, byte-checked by the test suite. Patches to source now go through exact-match edits only.

### 0.12 Reels for Instagram: workstream R, 2026-10-27 (planned, nothing built)

**The ask.** After AFTER ACTION #1 looked good: "how much of a change will this need to get this functional to the point where you can
pull out complete and edited Reels for Instagram from my streams?" Instagram is the channel's funnel (the rubric's first destination is
`GitanshTeckClips`), so this is where the finish work reaches an audience. Answered from the code and the workspaces, then added here as a
track. **Nothing in R is built yet.**

**What exists (checked 2026-10-27).**

| | State |
|---|---|
| Old reel stage (`clipbot reel`, `stages/reel.py`) | Works. 8 reels exist (Aug 5-10, three Kick workspaces, `cam_top`). Two frames of one looked at: facecam on top, game below, 1080x1920; no captions, no hook, no mastering to a target. It re-encodes from the VOD, so a workspace with no video on disk (every YouTube one) is refused ("Reels re-encode from the VOD"). Reels for YouTube were a non-goal of the *dashboard's reel editor* (it paints frames into a canvas); the edit engine has no such limit. |
| Streams | 27 Kick workspaces, each with `video.mp4` on disk (720p era); 8 YouTube workspaces, none with video (`embed`: cut straight from YouTube by `ytsegments`). |
| Picks waiting | 1,563 clips in `clips.json`. Kick 1,159 (1,016 pending, 139 approved, 3 rejected, 1 cut). YouTube 404 (314 pending, 73 cut as landscape clips, 12 approved, 5 rejected). **151 approved clips have never been made into reels.** |
| Edit engine, vertical today | Output preset `shorts_1080x1920` exists, but its framing is `blur` (the 16:9 picture over a blurred copy of itself). `canvas_chain` has `fit`/`fill`/`blur` and an optional crop in fractions applied first; **there is no stacked game-plus-cam layout.** `edit deliver`'s `shorts` variant reframes a finished master the same way (<= 180 s, audio copied). |
| What a reel would reuse | Sources from a Kick VOD or YouTube, cuts snapped to pauses and words, Hinglish word-timed captions, mastering, `verify`, versions and the review page, the webcam scene map, the 720 p source floor. |
| Not vertical-ready | Motion graphics are drawn 16:9 and left out of any other shape (with a warning). The `captions` block defaults to a box left of centre for the 16:9 stream layout. The scene map and safe camera assume the webcam sits inside a 16:9 frame. The review page at 9:16 has not been checked. |

**Phases** (sizes are estimates, in sessions like the earlier phases).

| Phase | Scope | Done when | Size |
|---|---|---|---|
| **R1: vertical layout** | `stacked` (game panel + facecam panel) and `cam_full` layouts, a per-stream layout profile, captions in a vertical safe zone, `edittemplates.reel` and `clipbot edit reel` for one clip. | An approved Kick talk clip, a Kick game clip and a YouTube clip each render as a 1080x1920 edit with captions, pass `verify`, and you have watched them. | about 1 |
| **R2: batch and delivery** | `clipbot edit reels` over a workspace's approved clips; a cover frame and `post.md` per reel; a folder of finished files; a `/reels` queue page (portrait player, approve/reject, the existing comment loop). | One command turns a workspace's approved clips into finished reels with post text, and the queue lists them. | about 1 |
| **R3: hook and polish** | A hook line over the first 1-2 s, 9:16 versions of the graphics, sound on the hook and impacts. | A batch you have seen where the hooks, plates and sound are things you would keep. | about 1, optional |
| R4: publishing | Instagram's publishing API. | not scheduled: ask first (see 3.R). | - |

**Order I recommend.** R1 and R2 are what produce "complete edited reels"; run them before Phase 5 (Sound) because they are the part that
reaches the audience. Decide R3 after you have seen a batch. 1a's Q2-Q5 are reporting tools; Q4 (a sources table in the report) fits R2,
the rest can wait until a clip looks wrong again.

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

### R. Reels for Instagram (vertical track; state and phases in 0.12)

**R1: vertical layout.**
- A clip's layout is `stacked {cam: top|bottom, split}`, `cam_full {focus}` or the existing `blur`. The panel rectangles come from a *layout profile* per stream
  style (`work/<slug>/layout.json` or a named profile in settings): where the game and the webcam sit in the source frame, as **fractions** (the `reelspec`
  convention, so a 720p and a 1080p copy of one layout agree). `editscene.find_cam_box` proposes the webcam box where its border is cyan; otherwise it is
  authored once per style. `reelspec.resolve` already does the stacked geometry as pure math: reuse it rather than write a second one.
- Order in the clip graph: retime -> fps -> **panels** (crop the game, crop the cam, scale each, stack) -> grade -> camera on the **game panel only** ->
  overlays -> captions. The cam is its own panel, so safe framing has nothing to protect in stacked mode. The clip fingerprint's `"v"` is bumped for the new
  graph and `tests/test_edit_framing.py` (real ffmpeg) must stay green.
- Captions get a vertical profile: centred, wide, large, inside a safe zone. Instagram's UI covers the bottom and the right edge (`reelspec.py` already
  keeps the chat box clear of the action column), but **the exact safe-zone numbers are not assumed: R1 looks up Instagram's current guidance and checks it on
  a rendered frame.**
- `edittemplates.reel`: a `clips.json` clip (or `workspace:start-end`) becomes a spec: in/out from the clip, snapped with `editcut.find_cut` and
  `editwords.word_snap`, captions on, the `shorts_1080x1920` output, the engine's master. Sources: a Kick VOD from disk, or YouTube through `ytsegments` with
  `edit.min_source_height` (720) still on.
- `verify` for 9:16: the caption box inside the safe zone, the length in range (the rubric says 20-60 s, never over 90), the existing static/black/loudness checks.
- Tests: spec round-trip, golden filter strings for the stacked graph, and a real-ffmpeg render at 1080x1920 that measures the panel positions on a
  two-colour synthetic source (as `test_edit_framing` does).

**R2: batch and delivery.**
- `clipbot edit reels --workspace W [--approved | --clips IDS] [--limit N]`: one edit per clip; fetches words (`whisper-1`, about $0.006 per audio minute; all 151
  approved clips at ~45 s each is roughly $0.70, an estimate) and captions (one Claude call per clip), renders a preview, and prints the estimated cost
  before spending, as `transcribe_openai` already does.
- Delivery: `work/_reels/<date>/<name>.mp4`, a 1080x1920 cover frame from the thumbnail compositor, and `<name>.post.md` (a caption in the channel's voice and
  hashtags, which you edit). Q4's sources table lands here.
- `/reels` page: vertical edits newest first, a portrait player, approve/reject, and the comment loop that already turns notes into spec changes.

**R3: hook and polish.**
- A hook line over the first 1-2 s, written from the clip's own words by a Claude call and kept in the spec (`hook`, editable). Only what is in the clip:
  the honesty rule that governs every number on screen applies to words too.
- `graphics/` takes a canvas size (today 1920x1080 is assumed in `render.mjs` and `editmotion.scale_for`); a 9:16 `Contact` plate and a `Hook` composition.
- Sound: a hook sting and impact cues from the library and `hudsounds.py`. A payoff-first cold open is an editorial choice per clip, never automatic.

**R4: publishing (not scheduled).** Two routes. You upload the finished files by hand (works for any file today), or the Instagram Graph API's content
publishing, which needs a Business or Creator account linked to a Facebook Page, a Meta app and a public URL for each video; I have not checked its
current limits. Posting is outward-facing, so it would be per post and only on your confirmation. A phone view of the review page is decision 9.

---

## 4. Phased roadmap

> **Superseded by the re-ordered roadmap in 0.4**: phase 0 is done, and the next phase is fixing the three problems from the v2 feedback (quality guard, safe dynamic framing, moment engine) before ears and captions. The table below is the original ordering, kept for the scope of each item.

| Phase | Scope | Why first | Size |
|---|---|---|---|
| **0: Quick wins** | B7 AMF previews; C1 voice chain on the mix; C4 join-level check and crossfades; A4 dead-picture detector (a contact sheet plus my review); D brand kit file plus fonts; F designed thumbnail v1; re-render Mish Mash #1 as v2. | Biggest visible and audible lift for little new machinery. Proves the loop on a video you've already seen. | 1-2 sessions |
| **1: Ears** | A1 word timestamps; A2 reaction map; B1 animated captions; H profanity and caption checks. | Captions and word-accurate cuts are the largest jump in watchability, and the reaction map closes my "can't hear" gap. | 2 sessions |
| **2: Review loop - BUILT 2026-10-25 (0.9)** | E1 review page with comments, E2 versions and changelog, E4 taste wiring, the `clipbot-review` skill. | Makes every later improvement iterate with you, not around you. | 1-2 sessions |
| **3: Look - B4, B5 built (0.10)** | B3 Remotion graphics kit; B4 punctuation macros; B5 transitions; B2 reaction framing with A5. | High-production feel; needs Phase 1 timing to place beats precisely. | 2-3 sessions |
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
7. **Re-transcribe streams with auto-detected language** (section 0.7)? The stage-3 transcript is Hindi-pinned and reads English as nonsense; it feeds `analyze`'s clip picking. About $1.66 per 4.6 h stream on whisper-1, and the existing `transcript.json` would be replaced (`transcribe --force`), so candidates found from it would need a re-run.
8. ~~A2 reaction map~~ - approved and built (section 0.8). **New question:** can you give me 10-20 moments you *know* you laughed or shouted in, with the stream and a rough time? That is the label the measurement lacks.
9. **Phone review?** The review page is dashboard-only (this PC). Do you want a hosted, phone-friendly version (a low-bitrate proxy video plus comments I read back), or is reviewing at the PC enough?
10. ~~**Graphics kit (B3):** OK to install Node 22+ and Remotion?~~ **Answered 2026-10-27: yes** ("go for it with node"). Built: section 0.11.
11. ~~**Beats in Mish Mash #1?**~~ **Answered 2026-10-27:** not proposals - a new video, in a new style, as a real run, with full creative control: AFTER ACTION #1 (0.11). Mish Mash #1 is untouched.
12. **Talk segments in a reel (R1):** face full-frame, or stacked over the screen? I will look at a frame of each recent YouTube stream first, so you only need to say what you want where there is nothing worth showing on screen.
13. **Reel style (R1-R3):** a plain, clean first batch (cuts, captions, mastering), or the HUD plates on gameplay reels too? A handle or watermark on every reel?
14. **Publishing (R4):** upload by hand from a folder (the default), or the Instagram API (a Business or Creator account, a Meta app, hosting)?
15. **Order:** run R1-R2 before Phase 5 (Sound)? I recommend yes (0.12).

## 6. Known limits (said up front)

- PyTorch on this PC runs on CPU: there's no ROCm on Windows. GPU acceleration comes through Vulkan (Real-ESRGAN ncnn, whisper.cpp) and AMF encoders, not torch.
- Separation, laughter and word alignment are **unmeasured on Hinglish speech over game audio**. Each one gets a small measured trial before it is trusted, the same way the kill-feed reader did.
- The 320x180 Kick webcam can't become a sharp close-up. Reaction framing helps most on YouTube and full-cam scenes.
- I'll still not hear the final the way you do. The loop is built so your notes, the reaction map and the analytics stand in for that, and every gap it can't close is reported, not hidden.
- **Reels (R):** a polished reel does not mean a reel that reaches people. The hook, the topic and when it is posted drive the feed, and nothing here measures that (E5 is YouTube-only).
- **Reels from Kick-era streams are 720p.** A stacked layout stretches the 320x180 webcam to the panel width (about 3.4x for a full-width panel), so the face is soft; 1080p YouTube streams do not have this problem. The old reels record this as `upscale` in `reel_output`.
- Instagram's own trending sounds and in-app effects are added inside Instagram, not by this tool (I have not checked whether its publishing API offers them).
- There is no face locator (A5): `cam_full` framing uses a `focus` point set per stream style, not tracked.
