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
   frame you looked at; otherwise write "unverified".
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
