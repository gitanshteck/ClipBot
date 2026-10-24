# Hard rules

Each rule is a failure that happened on this project (or, marked **[i-hate-editing]**,
one the MIT-licensed github.com/ranahaani/i-hate-editing recorded and this engine
already enforces). A hard rule has no "unless it looks better": if you think one is
wrong, change the rule here with the evidence, do not quietly break it in a spec.

## Truth

1. **Never present a moment you have not seen evidence for.**
   *Origin:* a nade kill at 3:23:37 was reported as confirmed from a transcript line;
   it was another player's kill, and the kill feed showed it. A reel built on that
   would have put someone else's play in Teck's montage. Use `killfeed.json` or a
   frame you looked at; otherwise write "unverified". A `candidates.json`
   description is Claude's reading of a noisy transcript, not evidence: a candidate
   saying "highest kill game ever, 23 kills" was a DEFEAT screen (second place) in
   the frame, and a "11x chat spike" was five chat messages at the start of a stream.
2. **Quote the number you measured, from the artefact you measured it in.**
   *Origin:* a stream start was stated as 09:59 UTC from a rounded figure; it was
   08:59:26. Read `verify.md` / `timeline.json` and copy from them.
3. **Say what you could not check.** A render you did not look at is "rendered, not
   inspected". The verifier cannot see taste, crops or a covered crosshair.
4. **Resolution is a property of the source.** Kick VODs are 720p60; a 1080p master
   of one is an upscale. Say so; never describe it as 1080p footage.

## Time

5. **Positions are source seconds, durations are output seconds.** Never absolute
   timeline times, never "N s into the video".
   *Origin:* the whole design; a callout authored in output time drifts off the kill
   the moment a ramp or an earlier trim changes the length.
6. **Anchor anything that follows a clip to that clip** (`on: {clip, at|marker}`).

## Picture

7. **Nothing on screen for longer than the profile's gap** (punchy 4 s, balanced 5 s,
   restrained 6.5 s). `edit validate` reports it; fix it in the spec, not by hope.
   *Origin:* a 6 s approach to a kill with no event sat inside the montage's first
   draft; the gate flagged it, and a slow push-in across lead-ups fixed it.
   **[i-hate-editing]** (beats gate)
8. **Something happens in the first 2 seconds.** **[i-hate-editing]**
9. **No flicker, no hold.** A callout is up 1-4 s. Under 1 s reads as a glitch; over
   4 s is a hold and the picture should have moved on.
10. **A callout never covers the action or the crosshair**; default `y=0.80`. Two
    callouts closer than 2.4 s stack (`y - 0.12`), never overlap.
11. **Never `linear` easing on a move the viewer should feel.** `out2` for arrivals,
    `inout2` for drifts. (`linear` only for holds.)
12. **Camera keys rise in time, one zoom pulse per kill, at most 4 per clip.** A fifth
    pulse is noise.
13. **A webcam is part of the frame, not an obstacle**: do not zoom into it, and do not
    put callouts on it. Check the storyboard.

## Sound

14. **Master to -14 LUFS, true peak under -1 dBTP, once, at the end.**
    *Origin:* the first reel shipped with a mean level of -26 dB: correct picture, a
    viewer reaching for the volume knob. The engine now measures and applies a linear
    second pass; do not disable `master.normalize` for a deliverable.
15. **The limiter ceiling sits 0.7 dB under the true-peak target.**
    *Origin:* with the ceiling at the target the AAC encode landed at -0.5 dBTP, over
    the limit the verifier enforces.
16. **Slow-motion audio is never a chain of tiny `atempo` pieces.**
    *Origin:* a per-0.1 s `atempo` chain dipped ~25 ms at every piece boundary (filter
    latency + concat's silence padding): an audible stutter across a ramp. One input,
    one grouped `atempo`, exact-length trim.
17. **Every cut gets a 30 ms audio fade**; **a click score over 14 at a join fails.**
18. **Sound effects land on their peak, not their start.** **[i-hate-editing]**
    A 0.4 s impact with its energy at 0.2 s must start 0.2 s early.
19. **Music is a bed, not a track: -18 dB, ducked under game audio, faded in/out.**
    Do not add music to a montage whose audio is the point (callouts, shots) without
    saying you did; the user may not want it.
20. **SFX density has a ceiling** (punchy ~5 per 15 s). More is clutter.

## Process

21. **Preview before final, final once.** Iterating on 1080p60 `slow` wastes minutes
    per try; a preview is seconds.
22. **Verify before you present; present what verify said.** `edit verify` after every
    final, and open `joins_01.jpg` and `loudness.png`.
    *Origin:* verify once flagged a title card as "black frames" - a detector bug found
    only because the artefact was read, not just the pass/fail line.
    **[i-hate-editing]** (verify gate)
23. **A render never overwrites `spec.json`.** Edit the spec deliberately; the render
    records its own copy under `render/`.
    *Origin:* a preview render replaced the author's spec with its 540p output block.
24. **Do not touch `reel.py`/`compile.py` behaviour from an edit task.** Report the
    problem (`reel.py` still passes `-filter_complex_script`, which ffmpeg 8 removed).
25. **Never invent a library asset id.** `clipbot library list`; the render fails
    loudly on an unknown id, which is correct.

## Cutting a roundup

30. **A cut lands in a pause, and a cut that matters to the words is checked, not
    trusted.** `edit episode` snaps every in/out into the nearest gap in the speech
    (`editcut.py`); a transcript timestamp alone is good to ~2 s.
    *Origin:* one snapped `out` landed 0.5 s inside the last word of a one-liner and
    another 0.1 s into a swear word the author meant to leave out. Found only by
    listing the pauses around each cut (`cuts.json` says what moved and what did not);
    pin those with `"snap": false` and a time read from the envelope. `edit words` is the
    check that names them (17 of 106 cut points in the first episode split a word; 6 are left
    after the word snap) and `edit episode --words` moves what can be moved.
31. **Match the clips' levels across sources.** `edit episode` sets a `gain_db` per
    clip (<= 8 dB) toward the lineup's median. *Origin:* clips from one lineup
    measured -22 to -35 dB loud-part level; unmatched, the quiet ones sit 10 dB under
    their neighbours and the master's single loudness pass cannot fix that.
32. **A card is never a silent gap, and nothing on one stays up past the dwell
    limit.** *Origin:* the 3 s sting card had 2.8 s of silence under it (verify flagged
    it) and the end card's text was up 6 s; the pacing issue was in `verify` but
    `validate` did not print it (it does now). Put a short bed or a hit under a card,
    and keep card text under 4 s.
33. **Do not draw a title into the picture until the author has chosen it.**
    *Origin:* the author wanted to watch the cut before naming it; a title baked in
    would have meant a re-render. A lineup's `title` stays in `notes`/`post.md` as a
    placeholder.
34. **When YouTube answers 429 or "not a bot", try another player client before
    asking for cookies.** `--extractor-args youtube:player_client=web_embedded` still
    returned the progressive 1080p60 formats `ytsegments` reads. Use a scratch
    `--settings` file; do not read the user's browser cookies without being asked.

35. **"Not frozen" is not "alive".** `edit verify` now measures real motion
    (static picture) because `freezedetect` passes any stream overlay. *Origin:* a
    10-minute episode had a loading screen, a black screen and a privacy-policy page
    of 8-15 s each and every check passed; I found them by reading contact sheets. A
    flagged stretch is a decision for the author (trim it, cover it with a graphic,
    or keep it because the audio is the joke), never silently ignored.
36. **Measure a tool before adopting it.** *Origin:* the plan proposed AMD's hardware
    encoder for previews; measured, `h264_amf` was *slower* (2.3 s) than x264
    ultrafast (1.4 s) on 20 s of 1080p60, because the preview cost is segment fetching
    and filtering, not encoding. Dropped, with the numbers in the plan.

37. **Never let a source quietly get worse.** A render that falls back to a lower-quality
    source is a failed render. *Origin:* an episode's third chapter was cut from 640x360
    because the YouTube client I had pinned stopped offering 1080p; the log said `format 18,
    640x360` and nothing failed, and `verify` passed, because it has no idea what the
    source looked like. The engine now refuses under `edit.min_source_height`; after any
    re-render **compare against the previous version** (per-clip sharpness) before
    presenting it.
38. **A zoom must not crop the webcam - and check where it zooms, not where you meant it
    to.** Every zoom in the first episode pivoted on the top-left corner (a filtergraph bug: crop
    read `iw` back from frames that change size), which I had read as "centred, and cropping the
    overlay"; it cropped the webcam in all 54 clips, and the layout changes with the stream's
    scene besides. Fixed twice over: the pivot, and `edit.safe_framing` (a scene map of the webcam
    box, camera keys whose window always holds it, overlays moved off it). `verify` now reads
    frames of the render for it (`webcam in frame`). *Origin of the second half:* I measured
    geometry that I assumed from the spec instead of from a rendered frame - the one number that
    would have shown the corner pivot was where a known box landed in the output.
39. **A clip starts where the action starts, not where the sentence does.** *Origin:*
    clips set from transcript lines began at the reaction ("mortar fell on me") instead of
    the hit, and kill windows were 6 s before / 3 s after: the kill without the fight.
    Include the lead-up and the aftermath, or mark the clip a deliberate cutaway.
40. **A transcript line says what was said, not what was on screen.** *Origin:* a "mortar
    landed on my face" clip ran over a quiet walk (he was retelling it from the last fight),
    "I hope you get him - and the kill comes" ended before the kill, and two Wardogs streams
    "had no kills" because a webcam covered the feed. Look at a contact sheet of the clip *and
    its surroundings* (`clipbot moments sheet --range`) before a clip goes in; a kill clip
    needs its lead-up and aftermath (`edit episode` flags it); and "no kills found" on a
    stream you know had a fight means *unreadable*, not *none* (`moments coverage`).
41. **Do not force the recogniser's language, and do not draw a caption it is unsure of.**
    *Origin:* word timing fetched with `language=hi` (the stream transcript's pin) wrote Devanagari
    nonsense over English speech and "Like share and subscribe" over game noise; 48% of the
    captions written from it were rated unsure. Auto-detect plus a Roman-script Hinglish prompt
    (`words.prompt`) cut that to 23%, and what is left is mostly rap lyrics and shouting over
    gunfire. A wrong subtitle is worse than none: `captions.min_confidence` leaves the unsure
    ones off, and you read `captions.json` before a render because nothing here can hear the
    clip. (One cost: a quiet window of real speech came back as emoji - `edit words` names such
    clips; their captions are written by hand or left out.)
42. **Know what is said in the first 30 seconds.** `edit captions` and `verify` list strong
    language there (the cold open of the first episode said "bastard" at 0:11). It is a
    decision for the author - bleep it, cut it, or keep it - not something to hide or to
    leave unmentioned in the report.
43. **A model's score is a hint until it has been checked against something that happened - and say
    which category you checked.** *Origin:* the reaction map (`clipbot events`, PANNs) tags every second of
    audio; I measured it before wiring it in (`clipbot events check` re-runs the measurement). **Gunfire** holds
    up against the kill record (AUC 0.74: 58% of kills score >= 0.25, against 20% of random moments).
    **Laughter** is a weak signal: against the chat's own laugh emotes (five Kick streams, 28-38 messages each)
    its AUC was 0.49, 0.61, 0.73, 0.61 and 0.64 - better than plain loudness on four of the five (mean 0.62
    against 0.54), nowhere near enough to pick a moment: of the 86 strongest peaks only 6 (7%) were followed by
    chat laughing, against 3% for a random moment, and the top peaks I read were game callouts ("spike hai",
    "win win win"). **Shouting** is barely ever detected (0-4 peaks in hours). So `moments.voice_weight` and
    `analyze.signals.events` are off. Never write "he laughs here" from `events peaks`: read the transcript and
    the chat at that second, and say what you could not check. The right fix is labels, not a better model:
    ten moments he *knows* he laughed in would settle it.

## Delivery

26. **YouTube chapters need 0:00, 3+ entries, each >= 10 s.** `edit validate` says
    whether they qualify; if not, say the description will not chapter.
27. **Shorts are <= 180 s.** `edit deliver` skips a longer variant and says so; do not
    cut the edit to fit without asking.
28. **A thumbnail is never within 0.3 s of a join.** (`deliver` enforces it.)
29. **Titles say what is in the video.** `title_options` builds them from the counts;
    do not write "insane" over 12 ordinary kills.

## How rules get added

When an edit goes wrong - the user complains, `verify` fails, you catch it in a
frame - write the rule *and the failure* here in the same turn, under the section it
belongs to, then check the spec/engine cannot repeat it (a gate in `edittimeline.pacing`
or `editqa.verify`, or a test) when that is cheap. A rule with no origin is a guess;
delete it if it never earns one.
